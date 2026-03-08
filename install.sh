#!/bin/bash

# Update system packages
sudo apt-get update
sudo apt-get upgrade -y

# Install system dependencies
sudo apt-get install -y python3 python3-pip python3-venv
    
# Create a virtual environment
python3 -m venv venv
#activate the virtual environment
source venv/bin/activate

# Install Python requirements
pip3 install -r requirements.txt
sudo apt install ffmpeg -y
echo "All dependencies for Raspberry Pi have been installed."

clear 

echo "================================"
echo "Running cam.py"
echo "================================"
python cam.py