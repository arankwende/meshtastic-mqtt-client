# meshtastic-mqtt-client
This is a simple program that accesses an MQTT server in which meshtastic routers publish messages and shows them on a gui.

The decoding of the protobuf messages is based on josh pirihi's great work here:
https://github.com/joshpirihi/meshtastic-mqtt



![Full page](https://user-images.githubusercontent.com/6488786/181139834-7cf71745-b981-492f-9545-8916f83b5ef7.png)


The script requires:

python and pip to be installed on the server 
for linux (apt):
```
sudo apt install python3 python3-pip
```
as well as the following modules with pip:

pyyaml, paho-mqtt, pySimpleGUI and protobuf :
```
sudo pip install pyyaml paho-mqtt pySimpleGUI protobuf
```

Once installed, just copy this repo (you can use git clone), complete the YAML file and rename it config.yaml in the /config/ folder, make the script executable and run it (chmod +x meshtastic-mqtt-client.py and ./meshtastic-mqtt-client.py).There's an example config file (config - example.yaml), you can directly modify and rename it config.yaml.
It must contain:

An MQTT configuration with IP, user, password, otherwise it will run until:
```
MQTT:
    MQTT_ip: MQTT BROKER IP
    MQTT_USER: 'MQTT user'
    MQTT_PW: 'MQTTPASSWORD'
```
Your Meshtastic configuration, basically channel id, client id and gateway id, as well as the information your client will provide to the network, a client full ID, a long name to identify you, a short name (3 characters) and a mac address:

```
MESHTASTIC:
    CHANNEL_ID: 'ShortFast' # the name of your channel
    CLIENT_ID: '12345678' #the id for your program to use
    GATEWAY_ID: '!12345678' # Your gateway's id
    CLIENT_FULL_ID: '!12345678'
    LONG_NAME: 'FULL_NAME'
    SHORT_NAME: 'TEST'
    MACADDR: '000000000000'
```
And you're set.
