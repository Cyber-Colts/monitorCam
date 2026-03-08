# MonitorCam

A high-performance, multi-camera MJPEG streaming server optimized for Raspberry Pi 4 and FRC (First Robotics Competition) applications.

## Features

- **Multi-camera support**: Stream from multiple USB cameras simultaneously
- **Low latency**: Optimized for real-time video streaming with minimal delay
- **FRC-optimized**: Uses standard FRC port conventions (5800-5810)
- **Hardware acceleration**: Supports Pi 4 VPU encoding when needed
- **Web interface**: Clean, responsive web UI for viewing camera streams
- **Snapshot capability**: Take still images from any camera
- **MJPEG streaming**: Efficient motion JPEG streaming for web browsers

## Architecture

The server supports multiple capture modes:

- **passthrough** (default): USB camera MJPEG bytes piped through unchanged with zero CPU re-encoding
- **hw**: Pi 4 VPU (VideoCore VI) hardware encoding to H.264
- **sw**: Software MJPEG fallback (not recommended for Pi 4)

## Requirements

- Raspberry Pi 4 (recommended) or other Linux system
- USB cameras supporting MJPEG or raw video
- Python 3.6+
- System dependencies: ffmpeg, ustreamer

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Cyber-Colts/monitorCam.git
   cd monitorCam
   ```

2. Run the installation script:
   ```bash
   ./install.sh
   ```

   This will:
   - Update system packages
   - Install Python 3 and pip
   - Create a virtual environment
   - Install Python dependencies (Flask, Waitress)

## Usage

### Basic Usage

```bash
python3 cam.py
```

### Advanced Options

```bash
# Hardware encoding mode
python3 cam.py --mode hw

# Custom port and resolution
python3 cam.py --port 5801 --width 640 --height 480 --framerate 30

# Multiple cameras
python3 cam.py --device /dev/video0 --device /dev/video1
```

### Command Line Options

- `--mode`: Capture mode (passthrough/hw/sw)
- `--port`: Server port (default: 5800)
- `--width`: Video width (default: 640)
- `--height`: Video height (default: 480)
- `--framerate`: Video framerate (default: 30)
- `--device`: Camera device path (can be specified multiple times)
- `--prefer-mjpeg`: Prefer MJPEG over H.264 (default: True)

## Web Interface

Once running, access the web interface at `http://<raspberry-pi-ip>:5800`

The interface provides:
- Live camera streams
- Individual camera views
- Snapshot download links
- Responsive grid layout

## API Endpoints

- `GET /`: Main camera overview page
- `GET /cam/<index>/`: Individual camera stream page
- `GET /cam/<index>/stream.mjpg`: MJPEG video stream
- `GET /cam/<index>/snapshot.jpg`: JPEG snapshot

## Configuration

### System Service

The project includes systemd service files for automatic startup:

- `ustreamer-cams.service`: Systemd service configuration
- `ustreamer-cams.conf`: Service configuration file

### Camera Configuration

Cameras are auto-detected, but you can specify specific devices:

```bash
python3 cam.py --device /dev/video0 --device /dev/video2
```

## Dependencies

### Python Packages
- Flask: Web framework
- Waitress: WSGI server

### System Packages
- ffmpeg: Video processing
- ustreamer: USB camera streaming
- python3-venv: Virtual environment support

## Troubleshooting

### No Cameras Detected
- Check USB connections: `ls /dev/video*`
- Verify camera compatibility with MJPEG
- Try different USB ports

### High Latency
- Use passthrough mode for MJPEG cameras
- Reduce resolution/framerate
- Check USB bandwidth usage

### Permission Issues
- Run with sudo for camera access
- Check user permissions for `/dev/video*` devices

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- Built for FRC Team Cyber-Colts
- Optimized for Raspberry Pi 4 hardware
- Uses ustreamer for efficient USB camera handling
