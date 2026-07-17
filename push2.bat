@echo off
cd /d "C:\Users\jomie\Documents\Github\agentic-system"
git add -A
git commit -F commit_msg.txt
git tag v0.3.0
git push origin main --tags
pause