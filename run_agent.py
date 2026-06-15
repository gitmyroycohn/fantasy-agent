"""Cowork wrapper -- runs the agent and writes output to logs/latest_output.md"""
import subprocess
import sys
import os
from datetime import datetime

os.makedirs("logs", exist_ok=True)

result = subprocess.run(
    [sys.executable, "-m", "agent.main", "--run", "daily"],
    capture_output=True,
    text=True
)

output = result.stdout + result.stderr
print(output)

with open("logs/latest_output.md", "w", encoding="utf-8") as f:
    f.write(f"# Agent Run -- {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
    f.write("```\n" + output + "\n```\n")
