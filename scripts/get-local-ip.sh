#!/bin/bash
# Get local IP address for iPhone access on same Wi-Fi network

# Try multiple methods to find local IP
if command -v hostname &> /dev/null && hostname -I &> /dev/null; then
    # Linux method 1
    IP=$(hostname -I | awk '{print $1}')
    if [ -n "$IP" ]; then
        echo "$IP"
        exit 0
    fi
fi

if command -v ip &> /dev/null && ip addr show &> /dev/null; then
    # Linux method 2 (more reliable)
    IP=$(ip addr show | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | cut -d/ -f1 | head -1)
    if [ -n "$IP" ]; then
        echo "$IP"
        exit 0
    fi
fi

if command -v ifconfig &> /dev/null; then
    # macOS/BSD method
    IP=$(ifconfig | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | head -1)
    if [ -n "$IP" ]; then
        echo "$IP"
        exit 0
    fi
fi

# Fallback: try to connect to a public IP to find which interface has internet
if command -v python3 &> /dev/null; then
    IP=$(python3 -c "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); ip = s.getsockname()[0]; s.close(); print(ip)" 2>/dev/null)
    if [ -n "$IP" ] && [ "$IP" != "127.0.0.1" ]; then
        echo "$IP"
        exit 0
    fi
fi

# Last resort
echo "192.168.x.x"
exit 1
