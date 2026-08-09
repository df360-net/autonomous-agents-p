# provision-agent.ps1 <agent-name> — everything a new agent needs that is NOT code.
#
#   1. a mailbox in Dovecot              (an agent is reached by email)
#   2. AGENT<N>_PASSWORD in .env         (it has to read that mailbox)
#   3. FLEET_HMAC_SECRET in .env, once   (so agents can prove who they are to each other)
#
# Secrets are generated HERE and written straight to .env. Nothing is printed, so this is safe
# to run over ssh and safe to paste into a chat log. Every step is idempotent: re-running
# changes nothing that already exists.
#
# The point of this file is that adding agent number thirty is one command rather than a
# runbook somebody follows slightly differently each time — the same argument as
# fleet_identity.py, one level up. Everything else about a new agent is six lines of compose.
#
#   ssh zeenie "powershell -NoProfile -File C:\Users\jianm\autonomous-agents\provision-agent.ps1 agent2"

param([Parameter(Mandatory=$true)][string]$Agent)

$ErrorActionPreference = 'Stop'
Set-Location 'C:\Users\jianm\autonomous-agents'
$envFile = '.env'
$key = ($Agent.ToUpper() -replace '-', '_') + '_PASSWORD'

function Get-EnvValue($name) {
    $line = Get-Content $envFile | Where-Object { $_ -like "$name=*" } | Select-Object -First 1
    if ($line) { return ($line -split '=', 2)[1] }
    return $null
}

function New-Secret($bytes) {
    # Generated inside the already-running agent container: it is the one place here with a
    # CSPRNG we already trust, and it keeps the value off the command line.
    return (docker exec agent1 python -c "import secrets;print(secrets.token_urlsafe($bytes))").Trim()
}

function Set-EnvValue($name, $value) {
    if (Get-EnvValue $name) { Write-Host "$name already in .env - leaving it alone"; return $false }
    Add-Content -Path $envFile -Value "$name=$value" -Encoding ascii
    Write-Host "$name written to .env"
    return $true
}

# --- 1 & 2: mailbox password ---------------------------------------------------
Set-EnvValue $key (New-Secret 24) | Out-Null
$pw = Get-EnvValue $key
if (-not $pw) { throw "could not read $key back out of $envFile" }

# `setup email add` updates an existing account's password rather than failing, which is what
# we want if .env was ever rewritten: the mailbox and the file agree afterwards either way.
$out = docker exec mailserver setup email add "$Agent@agents.local" $pw 2>&1
Write-Host "mailbox $Agent@agents.local ready"

# --- 3: the fleet key ----------------------------------------------------------
# Without it agent_principal.admit refuses every message claiming to come from an agent —
# correct with no peers, wrong the moment there are two.
Set-EnvValue 'FLEET_HMAC_SECRET' (New-Secret 32) | Out-Null

Write-Host ''
Write-Host 'done. Now:  schtasks /run /tn agents-deploy'
