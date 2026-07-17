@echo off
cd /d "C:\Users\jomie\Documents\Github\agentic-system"
echo === Git Status ===
git status
echo.
echo === Staging all changes ===
git add -A
echo.
echo === Committing ===
git commit -m "feat: v0.3.0 -- 8 council gates, breaker self-heal, security council, workflow engine, cron, sweeps, observability, CLI, examples, CI"
echo.
echo === Tagging ===
git tag v0.3.0
echo.
echo === Pushing ===
git push origin main --tags
echo.
echo === Done ===
pause