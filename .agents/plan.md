# Launcher Project Plan

## Overview
A keyword-based launcher for Windows that can open websites, applications, file explorer directories, and provide special features like calculator and code prettifier.

## Features
1. **Keyword-based launching**: Type a keyword to open a site, app, or directory
2. **Configurable mappings**: Add keywords and mappings via a config file (no code changes needed)
3. **Calculator**: Basic math operations
4. **Prettycode**: Split interface - input raw code on left, prettified code on right, easy copy
5. **Timesheet**: Open a URL in default browser
6. **Idea**: Open IntelliJ IDEA

## Tech Stack
- Python 3.x
- tkinter for GUI
- webbrowser for opening URLs
- subprocess for launching apps
- pygments for code prettifying (optional)
- json for config file

## Configuration File Format (config.json)
```json
{
  "mappings": {
    "timesheet": "https://example.com/timesheet",
    "idea": "C:/Program Files/JetBrains/IntelliJ IDEA Community Edition/bin/idea64.exe",
    "calc": "python -c \"import math; print(eval(input()))\""
  },
  "directories": {
    "docs": "D:/projects/docs",
    "downloads": "C:/Users/Name/Downloads"
  },
  "websites": {
    "github": "https://github.com",
    "stackoverflow": "https://stackoverflow.com"
  }
}
```

## UI Design
- Single input field at the top
- Results list below
- Double-click or Enter to execute
- Special keywords show special UI (e.g., prettycode opens split window)

## Implementation Phases
1. Phase 1: Basic keyword launching with config file
2. Phase 2: Calculator feature
3. Phase 3: Prettycode feature
4. Phase 4: Specific mappings (timesheet, idea)