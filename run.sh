#!/bin/bash
cd "$(dirname "$0")"
pip3 install -r requirements.txt -q
PORT=8780 python3 app.py
