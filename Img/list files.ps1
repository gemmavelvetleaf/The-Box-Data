# ---------------------------------------
# List file creation dates and names
# Runs in the folder the script is located in
# Saves output to list.txt
# ---------------------------------------

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutputFile = Join-Path $ScriptDir "list.txt"

Get-ChildItem -Path $ScriptDir -Recurse -File -ErrorAction SilentlyContinue |
    ForEach-Object {
       # "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f $_.CreationTime, 
        $_.FullName
    } |
    Out-File -FilePath $OutputFile -Encoding UTF8

Write-Host "File list saved to:"
Write-Host $OutputFile
