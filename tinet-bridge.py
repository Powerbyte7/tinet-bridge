import socket
import sys
import os
import dotenv
import threading
from colorama import init, Fore
import signal
import serial
import serial.threaded
import time
from serial.tools import list_ports
import logging

init(autoreset=True)

# ---------CONFIG--------- #
SERVER_ADDRESS = "tinethub.tkbstudios.com"
SERVER_PORT = 2052

SERIAL = True
DEBUG = True
MANUAL_PORT = False
ENABLE_RECONNECT = True
# -------END CONFIG------- #

logging.basicConfig(filename=f"log-{round(time.time())}.log",
                    filemode='a',
                    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.DEBUG)

logger = logging.getLogger()

CALC_ID = dotenv.get_key(key_to_get="CALC_ID", dotenv_path=".env")
USERNAME = dotenv.get_key(key_to_get="USERNAME", dotenv_path=".env")
TOKEN = dotenv.get_key(key_to_get="TOKEN", dotenv_path=".env")

if CALC_ID is None or USERNAME is None or TOKEN is None:
    print(Fore.RED + "calc ID, username or token could not be loaded from .env!")


def find_serial_port():
    while True:
        time.sleep(1)
        ports = list(serial.tools.list_ports.comports())
        for port in ports:
            if "USB Serial Device" in port.description or "TI-84" in port.description:
                return port
        

class SocketThread(threading.Thread):
    """Manages server connection"""

    def __init__(self):
        super(SocketThread, self).__init__()
        self.alive = False
        self.socket = None
        self.serial_manager = None
        self._lock = threading.Lock()
    
    def stop(self):
        self.alive = False

        if self.serial_manager.alive:
            self.serial_manager.write("internetDisconnected".encode())
            print("Notified client bridge got disconnected!")
        
        self.socket.close()
        self.join()

    def run(self):
        while self.serial_manager is None:
            pass

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        print("Creating TCP socket...")
        self.socket.settimeout(10)

        print("Connecting to TCP socket...")
        # try:
        self.socket.connect((SERVER_ADDRESS, SERVER_PORT))
        self.alive = True
       
        self.serial_manager.write("bridgeConnected\0".encode())
        print("Client got notified he was connected to the bridge!")

        while self.alive:
            server_response = bytes()
            try:
                server_response = self.socket.recv(4096)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Error: {e}")
                self.stop()

            if server_response is None or server_response == b"":
                logging.error(server_response)
                self.stop()
            decoded_server_response = server_response.decode()
            logging.debug(decoded_server_response)

            if DEBUG:
                print(f'R - server - ED: {server_response}')
            print(f'R - server: {decoded_server_response}')

            if self.serial_manager.alive:
                self.serial_manager.write(decoded_server_response.encode())
            
            print(f'W - serial: {decoded_server_response}')

            if decoded_server_response == "DISCONNECT":
                self.alive = False

    def write(self, data):
        """Thread safe writing (uses lock)"""
        with self._lock:
            return self.socket.send(data)


class SerialThread(threading.Thread):
    """Manages serial connection"""

    def __init__(self, serial_port):
        """\
        Initialize thread.

        Note that the serial_instance' timeout is set to one second!
        Other settings are not changed.
        """
        super(SerialThread, self).__init__()
        self.daemon = True
        self.serial_port = serial_port
        if MANUAL_PORT:
            self.serial = serial.Serial(self.serial_port, baudrate=9600, timeout=3)
        else:
            self.serial = serial.Serial(find_serial_port().device, baudrate=9600, timeout=3)
        self.socket_manager = None
        self.alive = True
        self._lock = threading.Lock()
        self._connection_made = threading.Event()

    def stop(self):
        """Stop the reader thread"""
        self.alive = False
        if hasattr(self.serial, 'cancel_read'):
            self.serial.cancel_read()
        self.join(2)

    def run(self):
        """Reader loop"""
        
        while self.alive and self.serial.is_open:
            try:
                # read all that is there or wait for one byte (blocking)
                data = self.serial.read(self.serial.in_waiting)
            except Exception as e:
                print(f"Error: {e}")
                if ENABLE_RECONNECT:
                    print("Trying to reconnect...")

                    while True:
                        time.sleep(1)
                        try:
                            if MANUAL_PORT:
                                self.serial = serial.Serial(self.serial_port, baudrate=9600, timeout=3)
                            else:
                                self.serial = serial.Serial(find_serial_port().device, baudrate=9600, timeout=3)
                            self.write("bridgeConnected\0".encode())
                            print("Reconnected!")
                            break
                        except Exception:
                            pass
                else:
                    self.alive = False
                    pass
            else:
                if data:
                    if data is None or data == b"":
                        logging.error("Data issue")
                    # make a separated try-except for called user code
                    decoded_data = data.decode().replace("/0", "").replace("\0", "")
                    logging.debug(decoded_data)
                    if DEBUG:
                        print(f'R - serial - ED: {data}')
                    print(f'R - serial: {decoded_data}')

                    self.socket_manager.write(decoded_data.encode())
                    print(f'W - server: {decoded_data}')

        self.alive = False

    def write(self, data):
        """Thread safe writing (uses lock)"""
        with self._lock:
            return self.serial.write(data)

    def close(self):
        """Close the serial port and exit reader thread (uses lock)"""
        # use the lock to let other threads finish writing
        with self._lock:
            # first stop reading, so that closing can be done on idle port
            self.stop()
            self.serial.close()

    def connect(self):
        """
        Wait until connection is set up and return the transport and protocol
        instances.
        """
        if self.alive:
            self._connection_made.wait()
            if not self.alive:
                raise RuntimeError('connection_lost already called')
            return (self, self.protocol)
        else:
            raise RuntimeError('already stopped')

    # - -  context manager, returns protocol

    def __enter__(self):
        """\
        Enter context handler. May raise RuntimeError in case the connection
        could not be created.
        """
        self.start()
        self._connection_made.wait()
        if not self.alive:
            raise RuntimeError('connection_lost already called')
        return self.protocol

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Leave context: close port"""
        self.close()


def receive_response(sock):
    sock.settimeout(0.1)
    try:
        response = sock.recv(4096)
        try:
            decoded_response = response.decode('utf-8').strip()
            if decoded_response.startswith("RTC_CHAT:"):
                print(Fore.MAGENTA + "Received RTC_CHAT:", decoded_response[len("RTC_CHAT:"):])
            elif decoded_response == "SERVER_PONG":
                print(Fore.CYAN + "Received SERVER_PONG")
            else:
                print(Fore.GREEN + "Received:", decoded_response)
            return decoded_response
        except UnicodeDecodeError:
            print(Fore.YELLOW + "Received non-UTF-8 bytes:", response)
    except socket.timeout:
        return None


def command_help():
    print("Available commands:")
    print("? - Show a list of all available (local) commands.")
    print("exit - Quit the terminal.")
    print("clear - Clear the terminal screen.")


def sigint_handler(sig, frame):
    print(Fore.RED + "\nCommand cancelled.")
    raise KeyboardInterrupt


# Returns all avaiable ports and prints them
def list_serial_ports():
    ports = list_ports.comports()
    for i, port in enumerate(ports):
        print(f"{i + 1}. {port.device} - {port.description}")
    return ports


# Prompts user to select a serial port
def select_serial_port():
    while True:
        ports = list_serial_ports()

        if len(ports) == 0:
            print("No devices detected! Is your calculator connected?")
            time.sleep(1)
            continue

        selected_index = input("Enter the number of the serial device you want to select: ")
        if selected_index == "":
            print("Please select a valid port!")
            sys.exit(1)
        if selected_index in [str(x + 1) for x in range(len(ports))]:
            port_number = int(selected_index) - 1
            print(port_number)
            return ports[port_number]
        else:
            print("Invalid selection. Please try again.")


def main():
    if SERIAL:
        try:
            print("\rInitiating serial...\n")

            selected_port = None
            if MANUAL_PORT:
                selected_port = select_serial_port()
            else:
                selected_port = find_serial_port()

        except serial.SerialException as err:
            if err.errno == 13:
                print("Missing USB permissions, please add them: ")
                print("sudo groupadd dialout")
                print("sudo usermod -a -G dialout $USER")
                print(f"sudo chmod a+rw {selected_port.device}")
                user_response = input("Add the permissions autmatically? (y or n): ").lower()
                if user_response == "y":
                    os.system("sudo groupadd dialout")
                    os.system("sudo usermod -a -G dialout $USER")
                    os.system(f"sudo chmod a+rw {selected_port.device}")
                sys.exit(1)
        
        socket_thread = SocketThread()
        serial_thread = SerialThread(selected_port.device)
        socket_thread.serial_manager = serial_thread
        serial_thread.socket_manager = socket_thread

        serial_thread.start()
        socket_thread.start()

        serial_thread.join()
        socket_thread.join()

        sys.exit(0)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print(Fore.LIGHTBLACK_EX + f"Connecting to {SERVER_ADDRESS}:{SERVER_PORT} ...")
        sock.connect((SERVER_ADDRESS, SERVER_PORT))
        print(Fore.GREEN + f"Connected to {SERVER_ADDRESS}:{SERVER_PORT} !")

        print(Fore.YELLOW + "Logging in..")
        sock.send("SERIAL_CONNECTED".encode())
        sock.recv(4096)

        sock.send(f"LOGIN:{CALC_ID}:{USERNAME}:{TOKEN}".encode())
        loggedIn = sock.recv(4096).decode().strip()

        if loggedIn != "LOGIN_SUCCESS":
            print(Fore.RED + "Login failed!")
            print(Fore.RED + loggedIn)
            sys.exit(1)
        print(Fore.GREEN + "Logged in as", USERNAME)

        print(Fore.CYAN + f"You are now connected to the socket server at {SERVER_ADDRESS}:{SERVER_PORT}.")
        print("Type your commands, and press Enter to send.")
        print("Type '?' to show a list of available commands.")

        signal.signal(signal.SIGINT, sigint_handler)

        while True:
            user_input = input(Fore.YELLOW + ">>> ")

            if not user_input.strip():
                print(Fore.YELLOW + "Please enter a command.")
                continue

            if user_input.lower() == "exit":
                break
            elif user_input.lower() == "clear":
                os.system("cls" if os.name == "nt" else "clear")
            elif user_input == "?":
                command_help()
            else:
                sock.send(user_input.encode())  # Encode the user input as bytes
                receive_response(sock)

    except KeyboardInterrupt:
        print(Fore.RED + "CTRL-C received. Command cancelled.")
    finally:
        sock.close()
        print(Fore.CYAN + "Socket connection closed.")

if __name__ == "__main__":
    main()