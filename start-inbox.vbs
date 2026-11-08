' Start the inbox hidden; exit if an instance is already running.
' Comments stay ASCII on purpose: wscript reads .vbs as ANSI, and UTF-8
' Chinese text can corrupt the script.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)

Set svc = GetObject("winmgmts:\\.\root\cimv2")
Set procs = svc.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='python.exe'")
For Each p In procs
    If Not IsNull(p.CommandLine) Then
        If InStr(p.CommandLine, "inbox.py") > 0 Then WScript.Quit
    End If
Next

sh.CurrentDirectory = dir

' Prefer the uv-managed virtualenv when it exists, fall back to system python.
venv = dir & "\.venv\Scripts\python.exe"
If fso.FileExists(venv) Then
    sh.Run "cmd /c """ & venv & """ inbox.py >> inbox.log 2>&1", 0, False
Else
    sh.Run "cmd /c python inbox.py >> inbox.log 2>&1", 0, False
End If
