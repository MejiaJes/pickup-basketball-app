# app.py
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import os
from dotenv import load_dotenv
from datetime import datetime
from twilio.rest import Client
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
TWILIO_PHONE = os.environ.get("TWILIO_PHONE")
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

    existing = supabase.table("players").select("player_id").eq("name", name).execute()
    if existing.data and len(existing.data) > 0:
        return jsonify({"status": "error", "message": "Player already exists."}), 400

    supabase.table("players").insert({"name": name, "phone_number": phone}).execute()
    return jsonify({"status": "ok", "message": "Player added successfully."})

# ------------------------------
# Game Type Selection
# ------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        session['game_type'] = request.form.get('game_type')
        return redirect(url_for('team_selection'))
    return render_template("game_type.html")

# ------------------------------
# Team Selection
# ------------------------------
@app.route("/team_selection", methods=["GET", "POST"])
def team_selection():
    game_type = session.get('game_type', '3v3')
    num_players = int(game_type[0])

    response = supabase.table("players").select("name").order("name").execute()
    players_list = [p["name"] for p in response.data]

    if request.method == "POST":
        data = request.get_json()

        # Extract players from form
        team_a, team_b = [], []
        for i in range(num_players):
            name_a = data.get(f"team_a_player{i+1}_name")
            phone_a = data.get(f"team_a_player{i+1}_phone")
            name_b = data.get(f"team_b_player{i+1}_name")
            phone_b = data.get(f"team_b_player{i+1}_phone")

            team_a.append(name_a)
            team_b.append(name_b)

            # If player not in DB, add them
            for name, phone in [(name_a, phone_a), (name_b, phone_b)]:
                if name and name not in players_list:
                    supabase.table("players").insert({
                        "name": name,
                        "phone_number": phone if phone else None,
                        "elo_rating": 1200
                    }).execute()
                    players_list.append(name)

        session['team_a'] = team_a
        session['team_b'] = team_b

        # Compute win probabilities
        team_a_elos = [supabase.table("players").select("elo_rating").eq("name", p).execute().data[0]["elo_rating"] for p in team_a]
        team_b_elos = [supabase.table("players").select("elo_rating").eq("name", p).execute().data[0]["elo_rating"] for p in team_b]

        avg_a = sum(team_a_elos)/len(team_a_elos) if team_a_elos else 1200
        avg_b = sum(team_b_elos)/len(team_b_elos) if team_b_elos else 1200

        session['win_prob_a'] = 1 / (1 + 10 ** ((avg_b - avg_a)/400))
        session['win_prob_b'] = 1 - session['win_prob_a']

        return redirect(url_for('live_logging'))

    return render_template("team_selection.html", num_players=num_players, players=players_list)

# ------------------------------
# Live Logging
# ------------------------------
@app.route("/live_logging", methods=["GET", "POST"])
def live_logging():
    team_a = session.get('team_a', [])
    team_b = session.get('team_b', [])

    if not team_a or not team_b:
        return redirect(url_for('team_selection'))

    win_prob_a = session.get('win_prob_a', 0.5)
    win_prob_b = session.get('win_prob_b', 0.5)

    return render_template(
        "live_logging.html",
        team_a=team_a,
        team_b=team_b,
        win_prob_a=win_prob_a,
        win_prob_b=win_prob_b
    )

# ------------------------------
# Finalize Game & ELO
# ------------------------------
def finalize_game_supabase(game_id, scores):
    team_totals = {"A":0, "B":0}
    for player_name, stats in scores.items():
        player_resp = supabase.table("players").select("*").eq("name", player_name).execute()
        if player_resp.data:
            player_id = player_resp.data[0]["player_id"]
        else:
            insert_resp = supabase.table("players").insert({"name": player_name, "elo_rating":1200, "wins":0,"losses":0}).execute()
            player_id = insert_resp.data[0]["player_id"]

        team = 'A' if player_name in session.get('team_a', []) else 'B'
        total_points = stats['points_1'] + 2*stats['points_2']
        team_totals[team] += total_points

        supabase.table("game_players").insert({
            "game_id": game_id,
            "player_id": player_id,
            "team": team,
            "points_1": stats['points_1'],
            "points_2": stats['points_2'],
            "total_points": total_points
        }).execute()

    winner_team = 'A' if team_totals['A'] > team_totals['B'] else 'B'
    supabase.table("games").update({
        "team_a_score": team_totals['A'],
        "team_b_score": team_totals['B'],
        "winner_team": winner_team,
        "finalized": True
    }).eq("game_id", game_id).execute()

    # Update Elo
    K = 32
    for player_name, stats in scores.items():
        player_data = supabase.table("players").select("*").eq("name", player_name).execute().data[0]
        player_id = player_data["player_id"]
        team = 'A' if player_name in session.get('team_a', []) else 'B'
        total_points = stats['points_1'] + 2*stats['points_2']

        team_players = session['team_a'] if team=='A' else session['team_b']
        opp_players = session['team_b'] if team=='A' else session['team_a']

        avg_team_elo = sum([supabase.table("players").select("elo_rating").eq("name", p).execute().data[0]["elo_rating"] for p in team_players])/len(team_players)
        avg_opp_elo = sum([supabase.table("players").select("elo_rating").eq("name", p).execute().data[0]["elo_rating"] for p in opp_players])/len(opp_players)

        expected = 1 / (1 + 10 ** ((avg_opp_elo - player_data["elo_rating"])/400))
        actual = 1.0 if team == winner_team else 0.0

        team_points_total = sum([scores[p]['points_1'] + 2*scores[p]['points_2'] for p in team_players]) or 1
        contribution = total_points / team_points_total

        elo_change = K * (actual - expected) * contribution
        new_elo = player_data["elo_rating"] + elo_change

        wins = player_data["wins"] + (1 if team==winner_team else 0)
        losses = player_data["losses"] + (1 if team!=winner_team else 0)

        supabase.table("players").update({
            "elo_rating": new_elo,
            "wins": wins,
            "losses": losses
        }).eq("player_id", player_id).execute()

    send_loss_notifications(game_id)

# ------------------------------
# Finalize Game Route
# ------------------------------
@app.route("/finalize_game", methods=["POST"])
def finalize_game():
    scores = request.get_json()
    game_resp = supabase.table("games").insert({"game_type": session.get("game_type","3v3")}).execute()
    game_id = game_resp.data[0]["game_id"]

    finalize_game_supabase(game_id, scores)
    return jsonify({"status":"ok", "game_id":game_id})

# ------------------------------
# Loss Notifications via Twilio (2 random losing players)
# ------------------------------
def send_loss_notifications(game_id):
    team_a_score = sum(gp['points_1'] + 2*gp['points_2'] for gp in supabase.table("game_players").select("*").eq("game_id", game_id).eq("team","A").execute().data)
    team_b_score = sum(gp['points_1'] + 2*gp['points_2'] for gp in supabase.table("game_players").select("*").eq("game_id", game_id).eq("team","B").execute().data)
    losing_team = 'A' if team_a_score < team_b_score else 'B'

    losing_players = supabase.table("game_players").select("*").eq("game_id", game_id).eq("team", losing_team).execute().data
    if not losing_players:
        return

    chosen_players = random.sample(losing_players, min(2, len(losing_players)))

    for gp in chosen_players:
        player = supabase.table("players").select("*").eq("player_id", gp["player_id"]).execute().data[0]
        if not player.get("phone_number"):
            continue

        message_body = f"Hey {player['name']}, tough game! Final Score: Team A {team_a_score} - Team B {team_b_score}. Your total: {gp['points_1'] + 2*gp['points_2']} pts."
        try:
            twilio_client.messages.create(
                to=f"whatsapp:{player['phone_number']}",
                from_=TWILIO_PHONE,
                body=message_body
            )
        except Exception as e:
            print(f"Failed to send message to {player['name']}: {e}")

# ------------------------------
# Leaderboard
# ------------------------------
@app.route("/leaderboard")
def leaderboard():
    players_data = supabase.table("players").select("player_id,name,elo_rating,wins,losses").execute().data
    player_map = {p["player_id"]: p["name"] for p in players_data}

    top_elo = sorted(players_data, key=lambda x: x["elo_rating"], reverse=True)[:5]

    top_total_points = supabase.table("game_players").select("player_id,total_points").execute().data
    total_points_map = {}
    for entry in top_total_points:
        pid = entry["player_id"]
        total_points_map[pid] = total_points_map.get(pid, 0) + entry["total_points"]

    top_total_points_list = [
        {"player_id": pid, "total_points": pts, "name": player_map.get(pid, "Unknown")}
        for pid, pts in total_points_map.items()
    ]
    top_total_points_list = sorted(top_total_points_list, key=lambda x: x["total_points"], reverse=True)[:3]
    all_total_points_list = [{"player_id": pid, "total_points": pts} for pid, pts in total_points_map.items()]

    for p in players_data:
        total_games = p["wins"] + p["losses"] or 1
        p["win_pct"] = p["wins"] / total_games
    top_win_pct = sorted(players_data, key=lambda x: x["win_pct"], reverse=True)[:3]

    top_2pt_data = supabase.table("game_players").select("player_id,points_2").execute().data
    points2_map = {}
    for entry in top_2pt_data:
        pid = entry["player_id"]
        points2_map[pid] = points2_map.get(pid, 0) + entry["points_2"]

    top_2pt_list = [
        {"player_id": pid, "points_2": pts, "name": player_map.get(pid, "Unknown")}
        for pid, pts in points2_map.items()
    ]
    top_2pt_list = sorted(top_2pt_list, key=lambda x: x["points_2"], reverse=True)[:3]
    all_players_2pt_list = [{"player_id": pid, "points_2": pts} for pid, pts in points2_map.items()]

    last_game_resp = (
        supabase.table("games")
        .select("*")
        .order("game_date", desc=True)
        .eq("finalized", True)
        .limit(1)
        .execute()
        .data
    )

    if last_game_resp:
        lg = last_game_resp[0]
        game_date = lg["game_date"].split("T")[0] if "T" in lg["game_date"] else lg["game_date"].split(" ")[0]

        last_game_scores = supabase.table("game_players").select("*").eq("game_id", lg["game_id"]).execute().data
        for ps in last_game_scores:
            ps["name"] = player_map.get(ps["player_id"], "Unknown")

        team_a_players = [p for p in last_game_scores if p["team"] == "A"]
        team_b_players = [p for p in last_game_scores if p["team"] == "B"]
    else:
        lg = None
        game_date = "N/A"
        team_a_players, team_b_players = [], []

    return render_template(
        "leaderboard.html",
        top_elo=top_elo,
        top_total_points=top_total_points_list,
        all_players_total=all_total_points_list,
        top_win_pct=top_win_pct,
        all_players_win_pct=players_data,
        top_2pt=top_2pt_list,
        all_players_2pt=all_players_2pt_list,
        last_game=lg,
        game_date=game_date,
        team_a_players=team_a_players,
        team_b_players=team_b_players,
    )

# ------------------------------
# Run Flask
# ------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
