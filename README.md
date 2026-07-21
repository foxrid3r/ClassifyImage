# ClassifyImage

ClassifyImage is a small desktop GUI for manually reviewing images, assigning each image to a class, and moving the classified files into corresponding subfolders.

## Features

- Browse to a folder containing JPG, JPEG, PNG, or BMP images.
- Define up to ten classification names.
- Classify with buttons or number keys `1` through `9`.
- Automatically advance after classification.
- Navigate with the left and right arrow keys.
- Zoom with the mouse wheel and pan by dragging.
- Play images automatically with a configurable delay.
- Move classified images into class-named subfolders.
- Warn before overwriting an existing destination filename.

## Requirements

- Python 3.10 or newer
- Tkinter, normally included with the Windows Python installer

## Setup on Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run

```powershell
python -m classify_image
```

Because the project uses a `src` layout, install it in editable mode first when running from a fresh clone:

```powershell
pip install -e .
classify-image
```

You can also run the included launcher:

```powershell
.\run.ps1
```

## Development

```powershell
pip install -r requirements-dev.txt
ruff check .
pytest
```

## Project layout

```text
ClassifyImage/
├── src/
│   └── classify_image/
│       ├── __init__.py
│       ├── __main__.py
│       └── app.py
├── tests/
│   └── test_package.py
├── .gitignore
├── LICENSE
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── run.ps1
└── README.md
```

## Notes

Classifications are held in memory until files are moved. Selecting a different source folder clears the current classification session.
