#!/bin/python 
# Las dependencias:
from ast import Not
from types import NoneType
import yaml
#import json for future version
import time
import os
import sys
import argparse
import paho.mqtt.client as mqtt
import logging
#import logging.handlers as handlers
import PySimpleGUI as sg
import sqlite3 as sl
#import sqlalchemy for future version
import random
#from queue import Queue
#from logging import NullHandler

#PROTOBUF dependencies copied from joshpirihi/meshtastic-mqtt
import portnums_pb2 as portnums_pb2
from portnums_pb2 import ENVIRONMENTAL_MEASUREMENT_APP, POSITION_APP
import mesh_pb2 as mesh_pb2
import mqtt_pb2 as mqtt_pb2
import environmental_measurement_pb2 as environmental_measurement_pb2
from google.protobuf.json_format import MessageToJson


# First we define the GUI
def generate_main_window(menu_def):
    sg.theme('Reddit')
    sg.set_options(element_padding=(1, 1))

    layout = [
             [sg.Menu(menu_def, tearoff=False, pad=(200, 1))],
             [sg.Text('Meshtastic MQTT Client')],
             [sg.Output(size=(80,25),key='-OUTPUT_RADIO-')],
             [sg.Button('Send Message to every node  ',key='-SEND-'), sg.InputText(key='-MSGINPUT-'),
             sg.Checkbox('Want Response',key='-WNTRSPTF-')],
             [sg.Button('Send Message to one node    '),sg.Text('message'),sg.InputText(size=(20,1),key='-NODE_MSG-'),
                sg.Text('Node'),sg.InputText(size=(10,1),key='-NODE-')],
             [sg.Button('Connect to MQTT Server        ', key='-CONNECT-')], [sg.Button('Disconnect from MQTT Server', key='-DISCONNECT-')]]

    return sg.Window('Meshtastic MQTT Client', layout, finalize=True, grab_anywhere=True)
def generate_gps_window(menu_def):
    sg.theme('Reddit')
    sg.set_options(element_padding=(1, 1))
    layout = [
             [sg.Menu(menu_def, tearoff=False, pad=(200, 1))],
             [sg.Text('GPS Client Positions')],
             [sg.Output(size=(80,25),key='-POSITIONS-')],
             [sg.Button('Export Positions',key='-EXPORT-')]]
    return sg.Window('GPS Client Positions', layout, finalize=True, grab_anywhere=True)
def load_config():
    try:
        config_dir = os.path.dirname(os.path.realpath(__file__))
        config_file = os.path.join(config_dir, 'config/config.yaml')
        configuration = open(config_file, 'r')
        logging.info(f"Opening the following file: {config_file}")
        config = yaml.safe_load(configuration)
        return config
    except Exception as exception:
        logging.critical(f"There's an error accessing your config.yml file, the error is the following: {exception}")
        print("There's no config yaml file in the program's folder, please check the logs.")
        sys.exit()
# Connect to MQTT funcionts - this I took directly from the paho docs.
def on_connect(client, userdata, flags, rc):
    if int(rc) == 0:
        logging.info(f"Succesfully connected to the server.The rc is {rc}.")
        client.subscribe("$SYS/#")
    elif int(rc) == 1:
        logging.info(f"The connection was refused due to an incorrect protocol version.The rc is {rc}.")
        print("The connection was refused due to an incorrect protocol version.") 
    elif int(rc) == 2:
        logging.info(f"The connection was refused due to an incorrect client identifier. The rc is {rc}.") 
        print("The connection was refused due to an incorrect client identifier.") 
    elif int(rc) == 3:
        logging.info(f"The connection was refused, the server is unavailable or there is mistake in the IP address.The rc is {rc}.") 
        print("The connection was refused, the server is unavailable or there is mistake in the IP address.The rc is {rc}.") 
    elif int(rc) == 4:
        logging.info(f"The connection was refused due to lack of authorization (wrong user or password).The rc is {rc}.")  
        print("The connection was refused due to lack of authorization (wrong user or password).")  
    elif int(rc) == 5:
        logging.info(f"The connection was refused due to lack of authorization (wrong user or password).The rc is {rc}.")  
        print("The connection was refused due to lack of authorization (wrong user or password).")  
def on_message(client, userdata, msg):
    if "$SYS/" in msg.topic: # I filter out the $SYS internal mqtt topic
        pass
    else:
        logging.debug("You have received the following message:" +str(msg.payload) + " on topic: " + str(msg.topic))
        decode_message(msg)
def decode_message(msg):
    try:
        full_message = msg.payload
#I add service envelope decoding from protobuf
        se = mqtt_pb2.ServiceEnvelope()

        se.ParseFromString(full_message)
        #print(dir(se.ParseFromString(full_message)))
        decoded_message = se.packet
#I use portnums to detect the type of message (text, position or nodeinfo)
        if str(getattr(decoded_message, "encrypted")) != "b''":
                    print("Encrypted message received.")
                    logging.info(getattr(decoded_message, "encrypted"))
                    logging.info("Encrypted message received, please check the settings on your Meshtastic uplink device.")
                    return None
        else:
            if decoded_message.decoded.portnum == portnums_pb2.TEXT_MESSAGE_APP:
                text_message = decoded_message.decoded.payload.decode("utf-8")
                mesh_node_id = str(getattr(decoded_message, "from"))
                if mesh_node_id == mesh_client_id:
                    pass
                else:
                    print(mesh_node_id + ": " + text_message)
                    main_window.Refresh()
                save_text_message(text_message, mesh_node_id)
            elif decoded_message.decoded.portnum == portnums_pb2.POSITION_APP:
                mesh_node_id = str(getattr(decoded_message, "from"))
                if mesh_node_id == mesh_client_id:
                    pass
                else:
                    decoded_packet = mesh_pb2.Position()
                    decoded_packet.ParseFromString(decoded_message.decoded.payload)
                    battery_level = decoded_packet.battery_level 
                    lat = decoded_packet.latitude_i * 1e-7
                    lon = decoded_packet.longitude_i * 1e-7
                    altitude= decoded_packet.altitude
                    node_time = decoded_packet.time
                    save_position(lat, lon, altitude, node_time, battery_level, mesh_node_id)
            elif decoded_message.decoded.portnum == portnums_pb2.NODEINFO_APP:
                mesh_node_full_id = mesh_pb2.User()
                mesh_node_id = str(getattr(decoded_message, "from"))
                if mesh_node_id == mesh_client_id:
                    pass
            else:
                mesh_node_full_id = mesh_pb2.User()
                mesh_node_id = str(getattr(decoded_message, "from"))
        
            save_full_message(full_message,mesh_node_id)
    except Exception as exception:
        logging.critical(f'There was an error decoding your received message. You get the following error: {exception} ')
def encode_message(message,mesh_channel_id,mesh_gateway_id,mesh_client_id):
    try:
        decoded_message = bytes(message,"ascii")
        encoded_message = mesh_pb2.Data()
        encoded_message.portnum = portnums_pb2.TEXT_MESSAGE_APP
        encoded_message.payload = decoded_message
        packet = mesh_pb2.MeshPacket()
        setattr(packet,"from",int(mesh_client_id))
        #setattr(packet,"to",random.getrandbits(32))
        setattr(packet, "to",4294967295)
        setattr(packet,"id",random.getrandbits(32))
        setattr(packet,"rx_time",1658889528)
        setattr(packet,"hop_limit",3)
        packet.decoded.CopyFrom(encoded_message)
        mesh_se = mqtt_pb2.ServiceEnvelope()
        mesh_se.channel_id = mesh_channel_id
        mesh_se.gateway_id = mesh_client_id
        mesh_se.packet.CopyFrom(packet)
        mesh_se=mesh_se.SerializeToString()
        return mesh_se
    except Exception as exception:
        logging.critical(f'There was an error encoding you message. You get the following error: {exception} ')   

def check_id(mesh_node_id):
    try:
        con = sl.connect('config/meshtastic-mqtt-client.db')
        cur = con.cursor()
        cur.execute(f'''SELECT COUNT(node_id) FROM NODE WHERE nodemeshid = '{str(mesh_node_id)}';''')
        sql_id_results = int(cur.fetchone()[0] or 0)
        if (sql_id_results == 0):
            logging.info("The mesh id that the message contained was not registered on the database. We will create a new record.")
            sqlite_query = f'''INSERT INTO NODE (nodemeshid) VALUES ('{str(mesh_node_id)}');'''
            cur.execute(str(sqlite_query))
            con.commit()           
            logging.info(f"{str(mesh_node_id)} has been inserted in the NODE table.")
            cur.execute(f'''SELECT node_id FROM NODE WHERE nodemeshid = '{str(mesh_node_id)}';''')
            node_id = str(cur.fetchone()[0])
        else:
            cur.execute(f'''SELECT node_id FROM NODE WHERE nodemeshid = '{mesh_node_id}';''')
            node_id = int(cur.fetchone()[0])
        return node_id
    except Exception as exception:
        logging.critical(f' There was a problem getting your node id. The exception is: {exception} ')
def save_full_message(full_message,mesh_node_id):
    try:
        node_id = check_id(mesh_node_id)
        con = sl.connect('config/meshtastic-mqtt-client.db')
        cur = con.cursor()
        sqlite_query = 'INSERT INTO MESSAGE (node_id,fullmessage) VALUES (?,?);'
        data_tuple = (node_id,str(full_message))
        cur.execute(str(sqlite_query), data_tuple)
        con.commit()
    except Exception as exception:
        logging.critical(f' There was a problem saving the full message to your database. The exception is: {exception} ')
def save_text_message(text_message, mesh_node_id):
    try:
        node_id = check_id(mesh_node_id)
        con = sl.connect('config/meshtastic-mqtt-client.db')
        cur = con.cursor()
        cur.execute(f''' INSERT INTO TEXTMESSAGE (node_id,textmessage) VALUES ('{node_id}','{text_message}');''')
        con.commit()
    except Exception as exception:
        logging.critical(f'There was an error saving your text message. The exception is: {exception} ')
def save_position(lat,lon,altitude,node_time,battery_level,mesh_node_id):
    try:
        node_id = check_id(mesh_node_id)
        con = sl.connect('config/meshtastic-mqtt-client.db')
        cur = con.cursor()
        cur.execute(f''' INSERT INTO POSITION (node_id,lat,long,altitude,node_time,battery) VALUES ('{node_id}','{lat}','{lon}', '{altitude}','{node_time}','{battery_level}');''')
        con.commit()
    except Exception as exception:
        logging.critical(f'There was an error saving your position, the exception is: {exception} ')
def on_publish(client, userdata, mid):
    logging.debug("the published message status:" + str(int(userdata or 0)) + " (0 means published)")
    logging.debug("the published message id is:" + str(mid))
def mqtt_publish_dict(mqtt_dict, client, mqtt_ip):
    for x, y in mqtt_dict.items():
        client.connect(mqtt_ip, 1883, 60)
        client.publish(str(x), str(y), qos=1, retain=True).wait_for_publish
        logging.debug("You have sent the following payload: " + str(y))
        logging.debug("To the following topic: " + str(x))
        logging.debug("On the server with IP: " + mqtt_ip)
def mqtt_publish_message(message,mqtt_topic,client,mqtt_ip,mesh_client_id):
    try:
        mesh_se = encode_message(message,mesh_channel_id,mesh_gateway_id,mesh_client_id)
        client.publish(str(mqtt_topic), mesh_se, qos=1, retain=True).wait_for_publish
        logging.debug("You have sent the following payload: " + str(message))
        logging.debug("To the following topic: " + str(mqtt_topic))
        logging.debug("On the server with IP: " + mqtt_ip)
    except Exception as exception:
        logging.critical(f'There was an error sending you message. You get the following error: {exception} ')   
def get_mqtt_config(config):
    try:
        mqtt_dict = config['MQTT']
        mqtt_ip = mqtt_dict['GATEWAY_ip']
        mqtt_user = mqtt_dict['MQTT_USER']
        mqtt_pass = mqtt_dict['MQTT_PW']
        period = mqtt_dict['TIME_PERIOD']
        logging.debug("This is your mqtt dictionary:" + str(mqtt_dict))
    except Exception as exception:
        logging.critical(f'Your YAML is missing something in the MQTT section. You get the following error: {exception} ')
    return mqtt_ip, mqtt_user, mqtt_pass, period
def get_meshtastic_config(config):
    try:
        meshtastic_dict = dict(config['MESHTASTIC'])
        mesh_channel_id = str(meshtastic_dict['CHANNEL_ID'])
        logging.debug(f"This is your channel id: {str(mesh_channel_id)}")
        mesh_client_id = str(meshtastic_dict['CLIENT_ID'])
        logging.debug(f"This is your client id: {str(mesh_client_id)}")
        mesh_gateway_id = str(meshtastic_dict['GATEWAY_ID'])
        logging.debug(f"This is your gateway id: {str(mesh_gateway_id)}")
        common_ports_dict = meshtastic_dict['COMMON_PORTS']
        extra_ports_dict = {}
        try:
            if "EXTRA_PORTS" in meshtastic_dict.keys():
                extra_ports_list = meshtastic_dict['EXTRA_PORTS']
                for port in extra_ports_list:
                    port_number = port['PORT_NUMBER']
                    port_name = port['PORT_NAME']
                    port_ha_class = port['HA_CLASS']
                    extra_ports_dict[port_number]= {"port_number": port_number, "port_name": port_name, "port_ha_class": port_ha_class}
                ports_count = len(extra_ports_dict)
                logging.info(f"You have {ports_count} extra ports configured.")
                logging.debug(f"This is the configuration information they have: {str(extra_ports_dict)}")
            else:
                logging.debug("You have no extra ports configured.")  
        except Exception as exception:
            logging.critical(f'Your YAML is missing something in the ports section. You get the following error: {exception} ')
        return mesh_channel_id, mesh_client_id, mesh_gateway_id, common_ports_dict, extra_ports_dict
    except Exception as exception:
        logging.critical(f'Your YAML is missing something in the Meshtastic section. You get the following error: {exception}')
def gateway_subscribe(client, mqtt_ip, common_ports_list, extra_ports_dict, mesh_channel_id, mesh_gateway_id):
    try:
        #for port in common_ports_list:
        mesh_mqtt_topic = "msh/1/c/" + str(mesh_channel_id) + "/#"  #+ "/"  + str(mesh_gateway_id) + "/" + port[0]
        client.subscribe(mesh_mqtt_topic)
        logging.info(f"You are now subscribed to {mesh_mqtt_topic}.")
    except Exception as exception:
        logging.error(f"There is an error in your power sensor collection. The error is the following: {exception}")
def client_db():
    try:
        con = sl.connect('config/meshtastic-mqtt-client.db')
        cur = con.cursor()
        cur.execute(''' SELECT count(name) FROM sqlite_master WHERE type='table' and name ='NODE' ''')
        if cur.fetchone()[0]==1 :
            print('NODE table exists.')
        else: 
            cur.execute("""CREATE TABLE NODE(node_id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, nodemeshid VARCHAR(255),Timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);""")
            print("NODE table created.")
        cur.execute(''' SELECT count(name) FROM sqlite_master WHERE type='table' and name ='MESSAGE' ''')
        if cur.fetchone()[0]==1 :
            print('MESSAGE table exists.')
        else: 
            cur.execute("""CREATE TABLE MESSAGE(message_id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, node_id INTEGER, fullmessage VARCHAR(255),Timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (node_id) REFERENCES NODE(node_id));""")
            print("MESSAGE table created.")
        cur.execute(''' SELECT count(name) FROM sqlite_master WHERE type='table' and name ='POSITION' ''')
        if cur.fetchone()[0]==1 :
            print('POSITION table exists.')
        else: 
            cur.execute("""CREATE TABLE POSITION(position_id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, node_id INTEGER, lat VARCHAR(255),long VARCHAR(255),altitude VARCHAR(255),node_time DATETIME, battery VARCHAR(255),Timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (node_id) REFERENCES NODE(node_id));""")
            print("POSITION table created.")
        cur.execute(''' SELECT count(name) FROM sqlite_master WHERE type='table' and name ='TEXTMESSAGE' ''')
        if cur.fetchone()[0]==1 :
            print('TEXTMESSAGE table exists.')
        else: 
            cur.execute("""CREATE TABLE TEXTMESSAGE(textmessage_id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, node_id INTEGER, textmessage VARCHAR(255),Timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (node_id) REFERENCES NODE(node_id));""")
            print("TEXTMESSAGE table created.")
        cur.execute(''' SELECT count(name) FROM sqlite_master WHERE type='table' and name ='GATEWAY' ''')
        if cur.fetchone()[0]==1 :
            print('GATEWAY table exists.')
        else: 
            cur.execute("""CREATE TABLE GATEWAY(gateway_id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, node_id INTEGER, nodemeshid VARCHAR(255),Timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (node_id) REFERENCES NODE(node_id));""")
            print("GATEWAY table created.")
        cur.execute(f''' SELECT count(node_id) FROM NODE WHERE nodemeshid  ='{mesh_client_id}' ''')
        if cur.fetchone()[0]==1:
            logging.info("Your node is already registered in the Database.")
        else:
            cur.execute(""" INSERT into NODE (nodemeshid) VALUES ('!7efeee00');""")
            con.commit()
            logging.info(f"Registering node {mesh_client_id} on your local database.")
    except Exception as exception:
        logging.critical(f"There was a problem starting you database. The exception is {exception}")
#def sql_db(): for future cases with external connection
def main(): # Here i have the main program
    """Main function to run window in a loop."""
    try:
    #Here I load yaml configuration files and create variables for the elements in the yaml
        global config
        config = load_config()
    except Exception as exception:
        logging.critical(f"Please check your YAML, it might be missing somethings. The exception is {exception}")
    #Create mqtt global variables
    global mqtt_ip, mqtt_user, mqtt_pass, period
    try:
        mqtt_ip, mqtt_user, mqtt_pass, period = get_mqtt_config(config)
    except Exception as exception:
        logging.critical(f"Please check your YAML, it might be missing some parts in the MQTT configuration. The exception is {exception}")
    try:
        global mesh_channel_id, mesh_client_id, mesh_gateway_id, common_ports_list, extra_ports_dict
        mesh_channel_id, mesh_client_id, mesh_gateway_id, common_ports_list, extra_ports_dict = get_meshtastic_config(config)
    except Exception as exception:
        logging.critical(f'Your YAML is missing something in the Meshtastic section. You get the following error: {exception} ')
# I define the ports list with all the meshtastic ports

    try:
        client_db()
    except Exception as exception:
        logging.critical(f"There was an error connecting to the Database. The exception is {exception}")
    global main_window
# I define my window menu and my first window    
    menu_def = ['File', ['Properties','Change Language - not yet implemented', 'Exit']],['Gateway', ['Gateway Information',' - not yet implemented']],['Messages',['See Crypto Key - Not yet implemented', 'List Known Nodes - not yet implemented', 'Export Message History - not yet implemented']],['GPS',['Open GPS Tab', 'Export GPS Locations']], ['&Help',['&Help', 'About...']]
    main_window = generate_main_window(menu_def)
    main_window.move(main_window.current_location()[0]-350, main_window.current_location()[1]+0)
    #output_window = main_window

    #I first copy methods according to Paho MQTT documentation, then I set the mqtt user and password (which is why I needed first the get_mqtt method, then I start the connection and finnally I create the network loop.)
    try:
        client = mqtt.Client("meshtastic-mqtt-user")
        client.on_message = on_message
        client.on_publish= on_publish
        client.username_pw_set(mqtt_user, password=mqtt_pass)
        client.enable_logger(logger=logger)
        client.on_connect = on_connect
        client.connect(mqtt_ip, 1883, 60)
        print("Connecting")
        client.loop_start()
        print("Succesfully connected to the MQTT Broker.")
        
    except Exception as exception:
        logging.critical(f"There seems to be a problem connecting to the mqtt server. The exception is {exception}")
    try:
        time.sleep(1)
        #I wait 1 second and subscribe to the mqtt_broker's gateway topic from my Meshtastic Gateway
        gateway_subscribe(client, mqtt_ip, common_ports_list, extra_ports_dict, mesh_channel_id, mesh_gateway_id)
    except Exception as exception:
        logging.critical(f"There seems to be a problem subscribing to the topics. The exception is {exception}")
    while(True):
            window, event, values = sg.read_all_windows()
            if sg.WIN_CLOSED:
                main_window.close()
                quit()
            #else:
                #window, event, values = sg.read_all_windows()
            if window == sg.WIN_CLOSED:
                force_exit = True
                client.disconnect
                client.loop_stop()
                window.close()
                quit()
            elif event == 'Exit':
                force_exit = True
                client.disconnect
                client.loop_stop()
                window.close()
                quit()
            elif event == sg.WIN_CLOSED:
                window.close()
            if event == 'Open GPS Tab':
                #If the Open GPS Tab option is clicked, I generate the GPS Window
                gps_window = generate_gps_window(menu_def)
                #And put it nice and tidy on the right of the main window
                gps_window.move(main_window.current_location()[0]+680, main_window.current_location()[1]+0)
            if event == '-DISCONNECT-':
                client.disconnect
                client.loop_stop()
                print("Disconnected from MQTT Server.")
                logging.info("Manually disconnected from MQTT Server.")
            if event == '-CONNECT-':
                try:
                    print("Connecting")
                    client.connect(mqtt_ip, 1883, 60)
                    client.loop_start()
                    gateway_subscribe(client, mqtt_ip, common_ports_list, extra_ports_dict, mesh_channel_id, mesh_gateway_id)
                    print("Succesfully connected to the MQTT Broker.")
                except Exception as exception:
                    logging.critical(f"There seems to be a problem connecting to the mqtt server. The exception is {exception}")
            elif event == '-SEND-': #if user clicks send message take input and send to radio
                try:
                    message=str(values['-MSGINPUT-'])
                    mqtt_topic = "msh/1/c/" + mesh_channel_id +"/" + mesh_gateway_id 
                    print(mesh_client_id + "(You): " + message)
                    mqtt_publish_message(message,mqtt_topic,client,mqtt_ip,mesh_client_id)
                except Exception as exception:
                    sg.popup(f'There was an error sending your message. The exception is the following: {exception}')

#We define some arguments to be parsed as well as help messages and description for the script.
parser = argparse.ArgumentParser(description='This is a simple python GUI that connects to an MQTT server to send and receive messages sent by a Meshtastic MQTT Gateway.')
parser.add_argument('-i', action='store_true', help='Run once to only create the entities in your MQTT broker (and see them in home assistant), in a silent initialization.')
parser.add_argument('-DEBUG', action='store_true', help='Add Debug messages to log.')
args = parser.parse_args()
#We define the logic and place where we're gonna log things
log_dir = os.path.dirname(os.path.realpath(__file__))  
log_fname = os.path.join(log_dir, 'config/meshtastic-mqtt-client.log') #I define a relative path for the log to be saved on the same folder as my py
formatter = logging.Formatter("[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s")
logger = logging.getLogger() # I define format and instantiate first logger
#fh = handlers.RotatingFileHandler(log_fname, mode='w', maxBytes=100000, backupCount=3)
#fh.setFormatter(formatter) 
#logger.addHandler(fh)
if getattr(args,'i'):
    logging.info("Started in silent iniatilization mode, so stopping now.")
    logging.info("Initialization complete.")
#    client.disconnect
#    client.loop_stop()
    quit()
if getattr(args,'DEBUG'):
    logger.setLevel(logging.DEBUG)  #I create an option to run the program in debug mode and receive more information on the logs
else:
    logger.setLevel(logging.INFO)
if getattr(args,'i'):
    logging.info("Running with -i in silent initialization mode. No GUI will open.")
if getattr(args,'DEBUG'):
    logging.info("Running with -DEBUG in DEBUG log mode.")
if __name__== '__main__':
        main()