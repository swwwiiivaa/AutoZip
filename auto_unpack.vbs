Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "pythonw.exe ""C:\path\to\auto_unpack.py"" watch --config ""C:\path\to\config.toml""", 0, False