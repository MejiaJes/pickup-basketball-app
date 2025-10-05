# app.py
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import os
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler
from supabase import create_client, Client as SupabaseClient
import random

# ------------------------------
# Load .env variables
# ------------------------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "replace-with-a-secure-key")

# ------------------------------
# Supabase client setup
# ------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------------------------
# Twilio setup
# ------------------------------
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = os.environ.get("TWILIO_PHONE")  # e.g. 'whatsapp:+14155238886'

twilio_client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)

# ------------------------------
# Player Management
# ------------------------------
@app.route("/add_new_player", methods=["POST"])
def add_new_player():
    data = request.get_json()
    name = data.get("name", "").strip()
    phone = data.get("phone", "").strip()

    if not name:
        return jsonify({"status": "error", "message": "Name is required."}), 400

    # Check if player exists
    existing = supabase.table("players").select("player_id").eq("name", name).execute()
    if existing.data and len(existing.data) > 0:
        return jsonify({"status": "error", "message": "Player already exists."}), 400

    # Insert new player
    supabase.table("players").insert({"name": name, "phone_number": phone}).execute()
    return jsonify({"status": "ok", "message": "Player added successfully."})

# ------------------------------
# Flask Routes
# ------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        session['game_type'] = request.form.get('game_type')
        # print("Selected game type:", session['game_type'])
        return redirect(url_for('team_selection'))
    return render_template("game_type.html")


@app.route("/team_selection", methods=["GET", "POST"])
def team_selection():
    game_type = session.get('game_type', '3v3')
    num_players = int(game_type[0])

    response = supabase.table("players").select("name").order("name").execute()
    players_list = [p["name"] for p in response.data]

    if request.method == "POST":
        session['team_a'] = [request.form.get(f"team_a_player{i+1}") for i in range(num_players)]
        session['team_b'] = [request.form.get(f"team_b_player{i+1}") for i in range(num_players)]
        return redirect(url_for('live_logging'))

    return render_template("team_selection.html", num_players=num_players, players=players_list)


@app.route("/live_logging", methods=["GET", "POST"])
def live_logging():
    team_a = session.get('team_a', [])
    team_b = session.get('team_b', [])
    return render_template("live_logging.html", team_a=team_a, team_b=team_b)

# ------------------------------
# Game Finalization & ELO
# ------------------------------
def finalize_game_supabase(game_id, scores):
    # Insert game
    supabase.table("games").insert({"game_type": session.get("game_type", "3v3")}).execute()

    # Insert game players
    for player_name, stats in scores.items():
        # Get or insert player
        player_resp = supabase.table("players").select("player_id").eq("name", player_name).execute()
        if player_resp.data:
            player_id = player_resp.data[0]["player_id"]
        else:
            insert_resp = supabase.table("players").insert({"name": player_name}).execute()
            player_id = insert_resp.data[0]["player_id"]

        team = 'A' if player_name in session.get('team_a', []) else 'B'
        total_points = stats['points_1'] + 2 * stats['points_2']
        supabase.table("game_players").insert({
            "game_id": game_id,
            "player_id": player_id,
            "team": team,
            "points_1": stats['points_1'],
            "points_2": stats['points_2'],
            "total_points": total_points
        }).execute()

    # Compute winner/loser
    team_a_score = sum(stats['points_1'] + 2*stats['points_2'] for p, stats in scores.items() if p in session.get('team_a', []))
    team_b_score = sum(stats['points_1'] + 2*stats['points_2'] for p, stats in scores.items() if p in session.get('team_b', []))
    winner_team = 'A' if team_a_score > team_b_score else 'B'
    losing_team = 'A' if winner_team == 'B' else 'B'

    # Update game with winner
    supabase.table("games").update({"winner_team": winner_team, "finalized": True}).eq("game_id", game_id).execute()

    # Update player stats & ELO
    for player_name, stats in scores.items():
        player_resp = supabase.table("players").select("*").eq("name", player_name).execute()
        player = player_resp.data[0]
        team = 'A' if player_name in session.get('team_a', []) else 'B'
        total_points = stats['points_1'] + 2*stats['points_2']

        # Wins/Losses
        if team == winner_team:
            supabase.table("players").update({"wins": player["wins"] + 1}).eq("player_id", player["player_id"]).execute()
        else:
            supabase.table("players").update({"losses": player["losses"] + 1}).eq("player_id", player["player_id"]).execute()

        # ELO
        K = 20
        team_total_points = sum(s['points_1'] + 2*s['points_2'] for p,s in scores.items() if (p in session['team_a'] if team=='A' else p in session['team_b'])) or 1
        contribution_fraction = total_points / team_total_points
        opponent_avg = 1200
        expected = 1 / (1 + 10 ** ((opponent_avg - player["elo_rating"]) / 400))
        actual = 1.0 if team == winner_team else 0.0
        elo_change = K * (actual - expected) * contribution_fraction
        new_elo = player["elo_rating"] + elo_change
        supabase.table("players").update({"elo_rating": new_elo}).eq("player_id", player["player_id"]).execute()

    # Create Supabase loss confirmations
    create_loss_confirmations(game_id)

# ------------------------------
# Create Loss Confirmations
# ------------------------------
def create_loss_confirmations(game_id):
    # Fetch game
    game_resp = supabase.table("games").select("*").eq("game_id", game_id).execute()
    if not game_resp.data:
        return
    game = game_resp.data[0]
    team_a_score = sum(gp['points_1'] + 2*gp['points_2'] for gp in supabase.table("game_players").select("*").eq("game_id", game_id).eq("team","A").execute().data)
    team_b_score = sum(gp['points_1'] + 2*gp['points_2'] for gp in supabase.table("game_players").select("*").eq("game_id", game_id).eq("team","B").execute().data)
    losing_team = 'A' if team_a_score < team_b_score else 'B'

    losing_players = supabase.table("game_players").select("player_id").eq("game_id", game_id).eq("team", losing_team).execute().data
    if not losing_players:
        return

    chosen_players = random.sample(losing_players, min(2, len(losing_players)))
    now_iso = datetime.now(timezone.utc).isoformat()
    for player in chosen_players:
        supabase.table("game_confirmations").insert({
            "game_id": game_id,
            "player_id": player["player_id"],
            "responded": False,
            "confirmed_loss": None,
            "created_at": now_iso,
            "updated_at": now_iso
        }).execute()

# ------------------------------
# Confirm Loss Route
# ------------------------------
@app.route("/confirm_loss/<int:confirmation_id>", methods=["GET", "POST"])
def confirm_loss(confirmation_id):
    message = None
    conf_resp = supabase.table("game_confirmations").select("*").eq("confirmation_id", confirmation_id).execute().data
    if not conf_resp:
        message = "Confirmation not found."
        return render_template("confirm_winner.html", confirmation_id=confirmation_id, message=message)

    confirmation = conf_resp[0]

    if request.method == "POST":
        response = request.form.get("response")
        update_data = {
            "responded": True,
            "confirmed_loss": True if response == "confirm" else False,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        supabase.table("game_confirmations").update(update_data)\
            .eq("confirmation_id", confirmation_id).execute()

        # Check confirmations
        confirmations = supabase.table("game_confirmations").select("*").eq("game_id", confirmation["game_id"]).execute().data
        confirmed = [c for c in confirmations if c["confirmed_loss"] == True]
        denied = [c for c in confirmations if c["confirmed_loss"] == False]

        if confirmed:
            finalize_loss(confirmation["game_id"])
        elif len(denied) >= 2:
            finalize_loss(confirmation["game_id"])

        return redirect(url_for("leaderboard"))

    return render_template("confirm_winner.html", confirmation_id=confirmation_id, message=message)

# ------------------------------
# Finalize Loss
# ------------------------------
def finalize_loss(game_id):
    game_resp = supabase.table("games").select("*").eq("game_id", game_id).execute()
    if not game_resp.data:
        return
    game = game_resp.data[0]

    team_a_score = sum(gp['points_1'] + 2*gp['points_2'] for gp in supabase.table("game_players").select("*").eq("game_id", game_id).eq("team","A").execute().data)
    team_b_score = sum(gp['points_1'] + 2*gp['points_2'] for gp in supabase.table("game_players").select("*").eq("game_id", game_id).eq("team","B").execute().data)
    losing_team = 'A' if team_a_score < team_b_score else 'B'
    winning_team = 'A' if losing_team == 'B' else 'B'

    supabase.table("games").update({"winner_team": winning_team, "finalized": True}).eq("game_id", game_id).execute()

    # Update stats
    losing_players_resp = supabase.table("game_players").select("player_id").eq("game_id", game_id).eq("team", losing_team).execute().data
    for lp in losing_players_resp:
        player_data = supabase.table("players").select("*").eq("player_id", lp["player_id"]).execute().data[0]
        supabase.table("players").update({"losses": player_data["losses"] + 1}).eq("player_id", lp["player_id"]).execute()

    winning_players_resp = supabase.table("game_players").select("player_id").eq("game_id", game_id).eq("team", winning_team).execute().data
    for wp in winning_players_resp:
        player_data = supabase.table("players").select("*").eq("player_id", wp["player_id"]).execute().data[0]
        supabase.table("players").update({"wins": player_data["wins"] + 1}).eq("player_id", wp["player_id"]).execute()

# ------------------------------
# Finalize Game Route
# ------------------------------
@app.route("/finalize_game", methods=["POST"])
def finalize_game():
    scores = request.get_json()

    game_resp = supabase.table("games").insert({"game_type": session.get("game_type","3v3")}).execute()
    game_id = game_resp.data[0]["game_id"]

    finalize_game_supabase(game_id, scores)
    send_loss_notifications(game_id)

    return jsonify({"status": "ok", "game_id": game_id})

# ------------------------------
# Loss Notifications via Twilio
# ------------------------------
def send_loss_notifications(game_id):
    team_a_score = sum(gp['points_1'] + 2*gp['points_2'] for gp in supabase.table("game_players").select("*").eq("game_id", game_id).eq("team","A").execute().data)
    team_b_score = sum(gp['points_1'] + 2*gp['points_2'] for gp in supabase.table("game_players").select("*").eq("game_id", game_id).eq("team","B").execute().data)
    losing_team = 'A' if team_a_score < team_b_score else 'B'

    losing_players = supabase.table("game_players").select("*").eq("game_id", game_id).eq("team", losing_team).execute().data
    for gp in losing_players:
        player = supabase.table("players").select("*").eq("player_id", gp["player_id"]).execute().data[0]
        if not player.get("phone_number"):
            continue
        total_points = gp["points_1"] + 2 * gp["points_2"]
        message_body = f"Hey {player['name']}, tough game! Final Score: Team A {team_a_score} - Team B {team_b_score}. Your total: {total_points} pts."
        try:
            twilio_client.messages.create(
                to=f"whatsapp:{player['phone_number']}",
                from_=TWILIO_PHONE,
                body=message_body
            )
        except Exception as e:
            print(f"Failed to send message to {player['name']} ({player['phone_number']}): {e}")

# ------------------------------
# Leaderboard
# ------------------------------
@app.route("/leaderboard")
def leaderboard():
    top_elo = supabase.table("players").select("name,elo_rating,wins,losses").order("elo_rating", desc=True).limit(5).execute().data
    return render_template("leaderboard.html", top_elo=top_elo)

# ------------------------------
# APScheduler: auto confirm unresponded losses
# ------------------------------
def auto_confirm_losses():
    now_utc = datetime.now(timezone.utc)
    one_hour_ago = now_utc - timedelta(hours=1)

    unresponded = supabase.table("game_confirmations").select("*")\
        .lt("created_at", one_hour_ago.isoformat())\
        .eq("responded", False).execute().data

    for gc in unresponded:
        supabase.table("game_confirmations").update({
            "responded": True,
            "confirmed_loss": True,
            "updated_at": now_utc.isoformat()
        }).eq("confirmation_id", gc["confirmation_id"]).execute()
        finalize_loss(gc["game_id"])

scheduler = BackgroundScheduler()
scheduler.add_job(auto_confirm_losses, 'interval', minutes=5)
scheduler.start()

# ------------------------------
# Run Flask
# ------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
