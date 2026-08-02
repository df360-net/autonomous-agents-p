@echo off
REM Run docker on Zeenie from the LOGGED-IN interactive session.
REM
REM Why this exists: Docker Desktop's CLI always resolves registry credentials through
REM docker-credential-desktop.exe, which reads the Windows credential vault. Over SSH you
REM get a *network* logon with no vault access, so every `docker pull`/`build` dies with
REM "A specified logon session does not exist" — even for public, anonymous images.
REM Launching this script via `schtasks /IT` runs it under the interactive token instead.
REM
REM Usage (from the main laptop):
REM   ssh zeenie "schtasks /run /tn agents-deploy"
REM then tail C:\Users\jianm\autonomous-agents\deploy.log

cd /d C:\Users\jianm\autonomous-agents
echo ==== deploy started %DATE% %TIME% ==== > deploy.log 2>&1
docker compose up -d --build >> deploy.log 2>&1
echo ==== exit %ERRORLEVEL% at %TIME% ==== >> deploy.log 2>&1
docker compose ps >> deploy.log 2>&1
