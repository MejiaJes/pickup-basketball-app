from collections import Counter
import re

file_path = "/Users/jesus/Downloads/_chat.txt"

message_counts = Counter()

# WhatsApp pattern: [date, time] sender:
pattern = r'^\[\d{1,2}/\d{1,2}/\d{2,4}, \d{1,2}:\d{2}:\d{2}\s*[AP]M\]\s*(.*?):'

with open(file_path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        match = re.match(pattern, line)
        if match:
            sender = match.group(1).strip()
            # Normalize sender: remove leading "~ " and extra spaces
            sender_name = sender.lstrip("~ ").strip()
            message_counts[sender_name] += 1

total_messages = sum(message_counts.values())

print(f"Total messages: {total_messages}\n")
print("Top 35 senders:\n")

top_35 = message_counts.most_common(35)

for sender, count in top_35:
    pct = (count / total_messages) * 100
    print(f"{sender}: {count} messages ({pct:.1f}%)")
    
# Generate tuples for top 25 senders
print("\n# Top 35 senders as tuples (name, message_count):")
for sender, count in top_35:
    # Escape single quotes in names for safety
    safe_sender = sender.replace("'", "''")
    print(f"('{safe_sender}', {count}),")

