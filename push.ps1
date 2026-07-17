# Push script for agentic-system
Set-Location "C:\Users\jomie\Documents\Github\agentic-system"

Write-Host "=== Git Status ===" -ForegroundColor Cyan
git status

Write-Host "`n=== Staging all changes ===" -ForegroundColor Cyan
git add -A

Write-Host "`n=== Committing ===" -ForegroundColor Cyan
git commit -m "feat: v0.3.0 -- 8 council gates, breaker self-heal, security council, workflow engine, cron, sweeps, observability, CLI, examples, CI"

Write-Host "`n=== Tagging ===" -ForegroundColor Cyan
git tag v0.3.0

Write-Host "`n=== Pushing ===" -ForegroundColor Cyan
git push origin main --tags

Write-Host "`n=== Done ===" -ForegroundColor Green
Read-Host "Press Enter to exit"