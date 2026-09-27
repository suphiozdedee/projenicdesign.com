import subprocess

ps_script = """
Add-Type -AssemblyName Microsoft.VisualBasic
$ret = [Microsoft.VisualBasic.Interaction]::AppActivate("Chrome")
Write-Host "Return value: $ret"
"""

result = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], capture_output=True, text=True, errors="replace")
print("STDOUT:", result.stdout.strip())
print("STDERR:", result.stderr.strip() if result.stderr else "")
