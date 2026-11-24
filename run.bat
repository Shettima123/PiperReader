@echo off
where pyw >nul 2>nul && (start "" pyw -3 "%~dp0piper_reader.pyw") || (start "" py -3 "%~dp0piper_reader.pyw")
