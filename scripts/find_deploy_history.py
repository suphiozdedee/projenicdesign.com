import glob
import re

for t in glob.glob(r'C:\Users\Projenic_Design\.gemini\antigravity\brain\*\.system_generated\logs\*.jsonl'):
    try:
        with open(t, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if 'deploy.js' in line:
                    for part in line.split('\\n'):
                        if 'deploy.js' in part:
                            print(part[:150])
    except Exception as e:
        pass
