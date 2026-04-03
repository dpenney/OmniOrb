param(
    [string]$HostIp = "192.168.4.34",
    [string]$User = "pi",
    [string]$Dest = "~/assistant/assistant_brains.py"
)

Write-Host "🚀 Syncing brain files to $User@$HostIp..." -ForegroundColor Cyan
scp ./pi/assistant_brains.py ${User}@${HostIp}:~/assistant/
scp ./pi/config.py ${User}@${HostIp}:~/assistant/
scp ./pi/HeyRobot.onnx ${User}@${HostIp}:~/assistant/

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Failed to copy the file. Ensure you are running this from the esp32_round root directory" -ForegroundColor Red
    exit 1
}

Write-Host "🔄 Restarting assistant.service on the Pi..." -ForegroundColor Cyan
ssh -o StrictHostKeyChecking=no ${User}@${HostIp} "sudo systemctl restart assistant.service && sudo systemctl status assistant.service"

if ($LASTEXITCODE -eq 0) {
    Write-Host "✅ Assistant successfully synced and restarted!" -ForegroundColor Green
} else {
    Write-Host "⚠️ Service restart might have failed. Check the status above." -ForegroundColor Yellow
}
