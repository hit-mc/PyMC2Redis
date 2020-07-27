# PyMC2Redis: Python Minecraft to Redis script

A chat message repeating script, written for MCDR.

This script transmits chat messages between my Minecraft server and a QQ bot. 

# Configuration

You should fill in this JSON object and save it as `pymc2redis.json` in the same folder with `MCDR.py`
```json
{
  "redis_server": {
    "address": "IP Address or Domain",
    "port": 6379,
    "password": "The password. If the Redis server uses anonymous authentication, remove this line."
  },
  "key": {
    "sender": "The sender key, Redis->MC",
    "receiver": "The receiver key, MC->Redis"
  }
}
```