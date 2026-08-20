@echo off
>"%~dp0last-args.txt" echo %*
echo {"type":"result","subtype":"success","is_error":false,"result":"SMOKE_OK","permission_denials":[]}
