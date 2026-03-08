#!/bin/bash

# Update system packages
sudo apt-get update
sudo apt-get upgrade -y

# Install system dependencies
sudo apt-get install -y python3 python3-pip python3-venv

# Install Python requirements
pip3 install -r requirements.txt

echo "All dependencies for Raspberry Pi have been installed."