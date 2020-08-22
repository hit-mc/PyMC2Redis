# -------------------------------------------------
# PyMC2Redis: Python Minecraft to Redis script
# Author: Keuin
# Version: 1.35 2020.08.22
# Homepage: https://github.com/keuin/pymc2redis
# -------------------------------------------------

import collections
import json
import re
import threading
import time
from threading import Lock
from threading import Thread

import redis
from redis import Redis

CONFIG_FILE_NAME = 'pymc2redis.json'
VERSION = '1.35 2020.08.22'

MESSAGE_THREAD_RECEIVE_TIMEOUT_SECONDS = 2  # timeout in redis LPOP operation
MESSAGE_THREAD_SLEEP_SECONDS = 0.5  # time in threading.Event.wait()
MESSAGE_SEND_MINIMUM_INTERVAL_SECONDS = 0.8
RETRY_SLOWDOWN_TARGET_SECONDS = 15
RETRY_SLOWDOWN_TIMES_THRESHOLD = 10
ALTERNATIVE_RETRY_SLOWDOWN_TIMES_THRESHOLD = 5
ALTERNATIVE_RETRY_SLOWDOWN_TARGET_SECONDS = 60

MSG_PREFIX = [' ', '#']
MSG_ENCODING = 'utf-8'
MSG_COLOR = '§7'
MSG_USER_COLOR = '§d'
MSG_SPLIT_STR = '||'

HEAD_PLAYER_LIST = 'SERVER'
HEAD_PLAYER_DIE = '悲報'
HEAD_PLAYER_ADVANCEMENT = '喜訊'

COLOR_GREEN = '§a'
COLOR_BLUE = '§9'
COLOR_YELLOW = '§e'
COLOR_RED = '§c'
COLOR_AQUA = '§b'

LOG_COLOR = '§e'
COMMAND_STATUS = '!PYMC'
COMMAND_RESET = '!PYMC reset'
RCOMMAND_LIST = '!LIST'  # Redis command

CFG_REDIS_SERVER = 'redis_server'
CFG_REDIS_SERVER_ADDRESS = 'address'
CFG_REDIS_SERVER_PORT = 'port'
CFG_REDIS_SERVER_PASSWORD = 'password'
CFG_KEY = 'key'
CFG_KEY_SENDER = 'sender'
CFG_KEY_RECEIVER = 'receiver'
CFG_LANGUAGE = 'lang'
CFG_TRANSLATION_SETTING = 'translating'
CFG_TRANSLATION_FROM = 'from'
CFG_TRANSLATION_TO = 'to'


# Simple logger wrapper
def log(text, prefix='LOG', ingame=False):
    message = '[PyMC2Redis][{pf}] {msg}'.format(pf=prefix, msg=text)
    if ingame and svr:
        svr.say('{color}{msg}'.format(msg=message, color=LOG_COLOR))
    if svr:
        svr.logger.info(message)
    else:
        print(message)  # fallback to STDOUT


def info(text, ingame=False):
    log(text, 'INFO', ingame)


def warn(text, ingame=False):
    log(text, 'WARN', ingame)


def error(text, ingame=False):
    log(text, 'ERROR', ingame)


# Simple test dyer

def green(s) -> str:
    return '{}{}'.format(COLOR_GREEN, s)


def yellow(s) -> str:
    return '{}{}'.format(COLOR_YELLOW, s)


def red(s) -> str:
    return '{}{}'.format(COLOR_RED, s)


def aqua(s) -> str:
    return '{}{}'.format(COLOR_AQUA, s)


# in-game message translator

def translate_format_item_value(a: str):
    """
    Preprocess: we replace all marks like %1$s to regex capture group (\S+)
    param a: The string to be processed.
    :return: The processed string.
    """
    i = 1
    pattern = '%{i}$s'
    desired = r'(\[.+\]|\S+)'
    while pattern.format(i=i) in a:
        a = a.replace(pattern.format(i=i), desired)
        i += 1
    return a


def translate(lang_from: dict, lang_to: dict, text: str):
    """
    Trnaslate a in-game message to a specific language.
    :param lang_from: the origin language.
    :param lang_to: the desired language.
    :param text: the message text to be translated.
    :return: a str object if translated, or None if failed.
    """

    # preprocess: we replace all marks like %1$s to regex capture group (\S+)

    for k, v in lang_from.items():
        lang_from[k] = translate_format_item_value(v)

    # start translating

    universe_key = None
    params = None

    for k, v in lang_from.items():
        # traverse all items and try to fit.
        r = re.fullmatch(v, text)
        if r:
            # The message fits this item!
            universe_key = k  # We use the key to identify this string
            params = r.groups()
            break

    # Now we have universe_key and params. We need to translate them into the target language.

    if not universe_key:
        # Oh no, we haven't found the universe key. Maybe the language setting is wrong?
        warn('Translator failed to match any items in the source language dict.')
        return None

    if universe_key not in lang_to:
        # The target language doesn't contain the key we need. Failed.
        warn('Translator failed to find desired item in the target language.')
        return None

    s = str(lang_to[universe_key])
    for i, real in enumerate(params):
        pattern = '%{i}$s'.format(i=i + 1)
        s = s.replace(pattern, real)

    return s


# Redis command related

class RCommand:
    """
    Base Redis command class.
    """
    _reply = None

    @staticmethod
    def from_redis_message(msg: str):
        """
        Build a RCommand object from a Redis message.
        :param msg: a message from the Redis server.
        :return: If the message is a valid Redis command, return a RCommand instance. Otherwise return None.
        """
        if RCOMMAND_LIST.lower() == str(msg).lower():
            return RCList()
        return None

    def get_echo(self):
        """
        Get the formatted echo. Note: is_valid_echo should be called and return True in advance.
        :return: The echo. If failed, return None.
        """
        if self._reply:
            return self._format_reply(self._reply)
        return None

    def is_valid_echo(self, message: str) -> bool:
        """
        After receiving a message from the server console, this method check if the message is a reply to this command instance.
        :return: True if the message is a reply to this command, thus this command instance should be executed and pop out. Otherwise, return False.
        """
        # This is a numb impl.
        # DO NOT RELY ON THIS
        self._reply = message
        return False

    def execute(self, server):
        pass

    def _format_reply(self, reply: str) -> str:
        """
        Format a valid reply from the server console to a friendly form.
        :param reply: The raw reply from server console.
        :return: The formatted message, which should be sent to the Redis as a response.
        """
        return reply


class RCList(RCommand):
    """
    Redis command that shows all online players.
    default command: #!list
    """

    def is_valid_echo(self, message: str) -> bool:
        if re.match(r'There are [0-9]+ of a max [0-9]+ players online:', message):
            self._reply = message
            return True
        return False

    def _format_reply(self, reply: str) -> str:
        r = re.findall(r'There are [0-9]+ of a max [0-9]+ players online:.*', reply)
        if not r:
            return 'Error: blank reply message.'
        return r[0]

    def execute(self, server):
        server.execute('list')


# Message management related (ADT/threads)

class MessageReceiverThread(Thread):
    """
    This thread receives messages from the Redis server, then print them on the in-game chat menu.
    """
    __quit_event = threading.Event()

    def __init__(self):
        Thread.__init__(self, name='MessageReceiverThread')

    def quit(self):
        self.__quit_event.set()

    def run(self):
        info('MessageReceiverThread is starting.')
        self.__quit_event.clear()
        global enabled, con, retry_counter, counter_message_to_game, rcommand, svr
        while enabled and con:
            try:
                if retry_counter.value() >= RETRY_SLOWDOWN_TIMES_THRESHOLD:
                    time.sleep(RETRY_SLOWDOWN_TARGET_SECONDS)  # cool down

                raw_message = con.brpop(
                    keys=config_keys[CFG_KEY_SENDER],
                    timeout=MESSAGE_THREAD_RECEIVE_TIMEOUT_SECONDS)
                if not raw_message:
                    continue  # Timed out. Possibly not a failure.
                if len(raw_message) != 2:
                    warn('Received invalid message from Redis server. Ignoring. ({})'.format(raw_message))
                    continue
                msg = Message.from_redis_raw_bytes(raw_message[1])
                if not msg:
                    warn('Cannot parse message: {}'.format(raw_message))
                    continue
                rcmd_instance = RCommand.from_redis_message(msg.get_body())
                if rcmd_instance:
                    # If the message is a valid Redis command
                    if not rcommand:
                        rcommand = rcmd_instance
                        rcmd_instance.execute(svr)
                    else:
                        warn(
                            'There is already a Redis command waiting for server response. The new command {} will be ignored.'.format(
                                msg.get_body()))
                else:
                    # The message is a normal chat msg. Just repeat it.
                    msg.print_ingame_message()

                # finish processing Redis message
                # update counters
                counter_message_to_game += 1  # The counter cares about all messages.
                retry_counter.reset()  # If we succeed, reset the cool-down counter
            except (ConnectionError, TimeoutError, redis.RedisError) as e:
                error('An exception occurred while waiting for messages from the Redis server: {}'.format(e))
                retry_counter.increment()
            if self.__quit_event.wait(MESSAGE_THREAD_SLEEP_SECONDS):
                break

        info('MessageReceiverThread is quitting.')
        info('MRT enabled={e}, con={c}'.format(e=enabled, c=con))


class Message:
    __sender = ""
    __body = ""

    def __init__(self, sender: str, body: str):
        self.__sender = sender
        self.__body = body

    def get_sender(self):
        return self.__sender

    def get_body(self):
        return self.__body

    @staticmethod
    def from_redis_raw_bytes(raw_bytes: bytes, encoding: str = MSG_ENCODING):
        """
        Construct a message from raw bytes received from Redis.
        :param raw_bytes: the raw bytes.
        :param encoding: the encoding.
        :return: the Message object. If failed, return None.
        """
        str_ = str(raw_bytes, encoding=encoding)
        r = re.match(r'([^|]*)(?:\|\|)(.*)', str_)
        if r and len(r.groups()) == 2:
            g = r.groups()
            return Message(g[0], g[1])
        return None

    @staticmethod
    def from_ingame_chat(raw_chat_str_with_prefix: str, sender: str):
        """
        Build a Message object with in-game chat string and sender ID.
        :param raw_chat_str_with_prefix: the chat string. such as '#Hello!'
        :param sender: the sender.
        :return: A Message. If the parameter is invalid, return None.
        """
        if not Message.is_outbound_message(raw_chat_str_with_prefix):
            return None
        return Message(sender, Message.__clean_message(raw_chat_str_with_prefix))

    @staticmethod
    def from_server_console_echo(echo: str, title='SERVER'):
        return Message(title, echo)

    @staticmethod
    def is_outbound_message(s: str) -> bool:
        """
        Check if this string should be transmitted to the Redis server.
        :param s: The string to be checked. Usually a raw chat string.
        :return: True or False.
        """
        for pf in MSG_PREFIX:
            if s.startswith(pf):
                return True
        return False

    def pack(self) -> str:
        """
        Pack the message to a string that can be pushed to Redis server.
        :return: the string.
        """
        return "{sender}{split}{msg}".format(sender=self.__sender, msg=self.__body, split=MSG_SPLIT_STR)

    def print_ingame_message(self):
        """
        Print this message on the in-game chat menu, with the default format.
        """
        if svr:
            svr.say(self.__to_ingame_string())

    @staticmethod
    def __clean_message(message: str) -> str:
        for pf in MSG_PREFIX:
            if message.startswith(pf):
                return message[len(pf):]
        return message

    def __to_ingame_string(self, msg_color=MSG_COLOR, user_color=MSG_USER_COLOR) -> str:
        return '{user_color}<{user}> {msg_color}{msg}'.format(
            msg_color=msg_color,
            msg=self.__body,
            user_color=user_color,
            user=self.__sender)


class MessageSenderThread(Thread):
    """
    Message sender provides a FIFO queue for message transmitting to the Redis server.
    Thus the message can be guaranteed to arrive the target server.
    """

    __queue = collections.deque()
    __queue_lock = Lock()
    __quit_event = threading.Event()

    def __init__(self):
        Thread.__init__(self, name='MessageSenderThread')

    def quit(self):
        self.__quit_event.set()

    def push(self, msg: Message) -> int:
        """
        Add a Message object into the queue.
        :param msg: the message.
        :return: the queue length.
        """
        self.__queue_lock.acquire(blocking=True)
        self.__queue.append(msg)
        size = len(self.__queue)
        self.__queue_lock.release()
        return size

    def length(self) -> int:
        self.__queue_lock.acquire(blocking=True)
        size = len(self.__queue)
        self.__queue_lock.release()
        return size

    def run(self):
        info('MessageSenderThread is starting.')
        self.__quit_event.clear()
        while enabled and con:

            # ---- loop start ----

            # peek
            self.__queue_lock.acquire(blocking=True)
            try:
                msg = self.__queue[0]
            except IndexError:
                msg = None
            self.__queue_lock.release()

            # send message
            if isinstance(msg, Message):
                if redis_send_message(msg):
                    # success, pop out
                    self.__queue_lock.acquire(blocking=True)
                    self.__queue.popleft()
                    self.__queue_lock.release()
            elif msg:
                warn('Bad object in message queue: {}'.format(msg))  # msg is not None and not a Message object
            # otherwise do nothing

            if self.__quit_event.wait(MESSAGE_SEND_MINIMUM_INTERVAL_SECONDS):
                break

            # ---- loop end ----

        info('MessageSenderThread is quitting.')
        info('MST enabled={e}, con={c}'.format(e=enabled, c=con))


class SafeCounter:
    __lock = Lock()
    __counter = 0

    def increment(self, increment: int = 1):
        self.__lock.acquire()
        self.__counter += increment
        self.__lock.release()

    def reset(self):
        self.__lock.acquire()
        self.__counter = 0
        self.__lock.release()

    def value(self) -> int:
        return self.__counter


# Main program

con = None
enabled = False
config_server = dict()
config_keys = dict()
language = {}
translating = {"from": "", "to": ""}
svr = None
receiver_thread = None
sender_thread = None
redis_reconnect_lock = Lock()
retry_counter = SafeCounter()
counter_message_to_game = 0
counter_message_to_redis = 0
counter_send_failure = 0
rcommand = None  # A RCommand waiting for response.


def redis_connect() -> bool:
    """
    Connect to the configured Redis server.
    :return: True if connected, False if failed to connect.
    """
    try:
        global con
        if config_server and CFG_REDIS_SERVER_ADDRESS in config_server and CFG_REDIS_SERVER_PORT in config_server:
            host = config_server[CFG_REDIS_SERVER_ADDRESS]
            port = config_server[CFG_REDIS_SERVER_PORT]
            password = config_server[CFG_REDIS_SERVER_PASSWORD] if CFG_REDIS_SERVER_PASSWORD else None
            info('Connecting to Redis server, host={host}:{port}, password=*****.'.format(host=host, port=port))
            con = Redis(
                host=host,
                port=port,
                password=password,
                socket_timeout=10,
                socket_connect_timeout=10,
                health_check_interval=15)
            info('Pinging...')
            return con.ping()
    except redis.RedisError:
        return False


def redis_reconnect():
    global retry_counter
    try:
        if redis_reconnect_lock.acquire(False):
            if retry_counter.value() >= RETRY_SLOWDOWN_TIMES_THRESHOLD:
                time.sleep(RETRY_SLOWDOWN_TARGET_SECONDS)  # cool down
            elif retry_counter.value() >= ALTERNATIVE_RETRY_SLOWDOWN_TIMES_THRESHOLD:
                time.sleep(ALTERNATIVE_RETRY_SLOWDOWN_TARGET_SECONDS)
            else:
                time.sleep(1)
            warn('Connection lost. Reconnecting to the Redis server... (retry_counter={cnt})'.format(
                cnt=retry_counter.value()), True)
            if redis_connect():
                info('Reconnected. Everything should run smoothly now.', True)
            else:
                info('Failed to reconnect to the specific Redis server.')
            retry_counter.increment()
    except Exception as e:
        error('Unexpected exception occurred while reconnecting: {}'.format(e))

    redis_reconnect_lock.release()


def redis_send_message(msg: Message) -> bool:
    """
    Send a message to Redis server.
    :param msg: the Message object.
    :return: True if success, False if failed.
    """
    global con, counter_message_to_redis, counter_send_failure
    broken_connection = False
    try:
        if con:
            if msg:
                # ---- push message start ----

                info('Pushing: {user}->{msg}'.format(user=msg.get_sender(), msg=msg.get_body()))
                r = con.lpush(config_keys[CFG_KEY_RECEIVER], msg.pack())
                try:
                    if isinstance(r, bytes) or isinstance(r, bytearray):
                        r = str(r, encoding=MSG_ENCODING)
                    numeric = int(r)
                    if numeric > 0:
                        info('Success.')
                        counter_message_to_redis += 1
                        return True
                    else:
                        info('Failed when pushing message: queue_length={}, raw_response={}'.format(numeric, r))
                except ValueError:
                    error('Invalid response: {}'.format(r))

                # ---- push message end ----
            else:
                # msg is None
                error('This should not happen. Please report this to Keuin.')
        else:
            error('Broken connection. Cannot talk to Redis server.', True)
            broken_connection = True

    except (ConnectionError, TimeoutError, redis.RedisError) as e:
        error('Failed to talk to the Redis server: {}.'.format(e))
        broken_connection = True
    except Exception as e:
        error('Unexpected exception: {}'.format(e))

    if broken_connection:
        redis_reconnect()
    counter_send_failure += 1
    return False


def redis_ping() -> bool:
    if not con:
        return False
    try:
        return con.ping()
    except redis.RedisError:
        return False


# def parse_redis_command(message):
#     """
#     Check if a message from Redis is a command. If so, execute it. Otherwise do nothing.
#     :return: A string if the given message is a valid Redis command, and it has been executed. False if the message is not a Redis command.
#     """
#     message = str(message).lower()
#     if message == RCOMMAND_LIST.lower():
#         # list players


def init() -> bool:
    """
    Clean-up, load config file, connect to Redis server and start message threads.
    :return: True if success, False if failed to initialize.
    """
    global con, enabled, config_server, config_keys, language, translating, receiver_thread, sender_thread
    global redis_reconnect_lock, retry_counter, counter_message_to_game, counter_message_to_redis, counter_send_failure, rcommand

    # reset connection
    if con:
        con.close()
    con = None
    if isinstance(receiver_thread, MessageReceiverThread) and receiver_thread.is_alive():
        receiver_thread.quit()

    if isinstance(sender_thread, MessageSenderThread) and sender_thread.is_alive():
        sender_thread.quit()

    # reset globals
    receiver_thread = None
    sender_thread = None
    redis_reconnect_lock = Lock()
    retry_counter = SafeCounter()
    counter_message_to_game = 0
    counter_message_to_redis = 0
    counter_send_failure = 0
    rcommand = None

    # read configuration and connect
    try:
        with open(CONFIG_FILE_NAME, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except FileNotFoundError:
        error('Cannot locate configuration file {}.'.format(CONFIG_FILE_NAME))
        return False
    except IOError as e:
        error('Encountered an I/O exception while reading configuration file {f}: {e}'.format(f=CONFIG_FILE_NAME, e=e))
        return False

    # --- check config ---

    # check server
    if CFG_REDIS_SERVER not in config:
        error('Cannot read redis server info from {}.'.format(CONFIG_FILE_NAME))
        return False
    config_server = config[CFG_REDIS_SERVER]
    if CFG_REDIS_SERVER_ADDRESS not in config_server \
            or CFG_REDIS_SERVER_PORT not in config_server:
        error('Redis server address or port is not defined. Check {} file.'.format(CONFIG_FILE_NAME))
        return False

    # check keys
    if CFG_KEY not in config:
        error('Cannot read keys from {}.'.format(CONFIG_FILE_NAME))
        return False
    config_keys = config[CFG_KEY]
    if CFG_KEY_SENDER not in config_keys \
            or CFG_KEY_RECEIVER not in config_keys:
        error('Cannot read keys.sender or keys.receiver from the configuration.')
        return False

    # check and read the translation and languages

    language = config[CFG_LANGUAGE]
    translating = config[CFG_TRANSLATION_SETTING]

    if not isinstance(language, dict):
        error('Malformed language dict in the configuration.')
        return False

    if not isinstance(translating, dict) or 'from' not in translating or 'to' not in translating:
        error('Invalid translating setting in the configuration.')
        return False

    # Validate needed languages

    if translating['from'] not in language:
        error('The language {} of translating.from is not defined.'.format(translating['from']))
        return False

    if translating['to'] not in language:
        error('The language {} of translating.to is not defined.'.format(translating['to']))
        return False

    # --- check config ---

    # connect to Redis host
    if redis_connect():
        info('Connected.')
    else:
        error('Failed to connect to Redis server. Please check your settings and network.', True)
        con = None
        return False

    # start threads
    enabled = True
    receiver_thread = MessageReceiverThread()
    sender_thread = MessageSenderThread()
    receiver_thread.start()
    sender_thread.start()

    return True


def enable():
    """
    Initialize the plugin, and print error messages if failed to enable it.
    :return:
    """
    global enabled
    enabled = init()
    if not enabled:
        error('Due to an earlier error, PyMC2Redis will be disabled.'
              ' Please check your configuration and type "{cmd}" to reset.'.format(cmd=COMMAND_RESET), True)


def disable():
    global enabled, redis_reconnect_lock

    enabled = False

    if isinstance(sender_thread, MessageSenderThread) and sender_thread.is_alive():
        info('Stopping sender thread.')
        sender_thread.quit()
        sender_thread.join()

    if isinstance(receiver_thread, MessageReceiverThread) and receiver_thread.is_alive():
        info('Stopping receiver thread.')
        receiver_thread.quit()
        receiver_thread.join()

    if isinstance(con, Redis):
        info('Closing connection.')
        con.close()

    redis_reconnect_lock = Lock()  # Generate a new lock to prevent unexpected deadlock.


def on_load(server, old_module):
    global enabled, svr, receiver_thread
    svr = server
    enable()


def on_unload(server):
    global enabled, con, receiver_thread
    if not enabled:
        return
    enabled = False
    if receiver_thread:
        receiver_thread.quit()
    if con:
        con.close()


def on_info(server, info_):
    global rcommand
    if info_.is_user or not isinstance(rcommand, RCommand):
        # We just care about Redis command echo in this procedure.
        # For user messages, we process them in on_user_info().
        return
    msg = str(info_.content)
    if rcommand.is_valid_echo(msg):
        if isinstance(sender_thread, MessageSenderThread):
            echo = rcommand.get_echo()
            if echo:
                sender_thread.push(Message.from_server_console_echo(echo, HEAD_PLAYER_LIST))
            else:
                error('Invalid echo from the server console.')
            rcommand = None  # Remove the command
        else:
            error('Sender thread is not alive, the pending Redis command cannot be executed.')


def on_user_info(server, info_):
    msg = str(info_.content)
    player = str(info_.player)
    if Message.is_outbound_message(msg):
        # If the message sent by a player is a valid outbound message (to Redis).
        # Message
        message = Message.from_ingame_chat(msg, player)
        if isinstance(sender_thread, MessageSenderThread):
            # If the message sender thread is alive.
            sender_thread.push(message)
        else:
            error('Message sender thread is not alive. Cannot repeat outbound message.')
    elif msg.upper() == COMMAND_RESET.upper():
        # !PYMC reset
        info(aqua('Disabling...'), True)
        disable()
        info(aqua('Enabling...'), True)
        r = init()
        if r:
            info(aqua('Reloaded. Type "{cmd}" to check working status.'.format(cmd=COMMAND_STATUS)), True)
        else:
            info(red('Failed to reload. Report this to Keuin.'), True)
    elif msg.upper() == COMMAND_STATUS.upper():
        # !PYMC
        server.say('Waiting for ping response...')

        # ping
        ping = redis_ping()
        if ping:
            ping = green('Fine')
        else:
            ping = red('Timed Out')

        # check threads

        sender_alive = green('Alive')
        if not isinstance(sender_thread, MessageSenderThread):
            sender_alive = red('N/A')
        elif not sender_thread.is_alive():
            sender_alive = red('Dead')

        receiver_alive = green('Alive')
        if not isinstance(receiver_thread, MessageReceiverThread):
            receiver_alive = red('N/A')
        elif not sender_thread.is_alive():
            receiver_alive = red('Dead')

        queue_len = red('N/A')
        if isinstance(sender_thread, MessageSenderThread):
            queue_len = sender_thread.length()
            if queue_len < 0 or queue_len > 4:
                queue_len = red(queue_len)
            elif queue_len <= 1:
                queue_len = green(queue_len)
            else:
                queue_len = yellow(queue_len)

        server.say((''
                    '==== PyMC2Redis Status ====\n'
                    'Version: {ver}\n'
                    'Ping: {ping}\n'
                    'Sender Thread: {sender}\n'
                    'Receiver Thread: {receiver}\n'
                    'Queue Length: {queue_len}\n'
                    'Counter (in/out/failed): {counter_in}/{counter_out}/{counter_failed}\n'
                    '==== PyMC2Redis Status ====').format(
            ver=VERSION,
            ping=ping,
            counter_in=counter_message_to_game,
            counter_out=counter_message_to_redis,
            counter_failed=counter_send_failure,
            queue_len=queue_len,
            sender=sender_alive,
            receiver=receiver_alive
        ))


# def on_player_joined(server, player):
#     if not enabled:
#         return
#     msg = Message.from_server_console_echo('{} joined the game.'.format(player), '登录')
#     if isinstance(sender_thread, MessageSenderThread):
#         # If the message sender thread is alive.
#         sender_thread.push(msg)
#
#
# def on_player_left(server, player):
#     if not enabled:
#         return
#     msg = Message.from_server_console_echo('{} left the game.'.format(player), '离开')
#     if isinstance(sender_thread, MessageSenderThread):
#         # If the message sender thread is alive.
#         sender_thread.push(msg)


def on_death_message(server, death_message):
    if not enabled:
        return
    translated_death_message = translate(language[translating[CFG_TRANSLATION_FROM]],
                                         language[translating[CFG_TRANSLATION_TO]], death_message)
    log('translation: {} -> {}'.format(death_message, translated_death_message))
    msg = Message.from_server_console_echo(translated_death_message if translated_death_message else death_message,
                                           HEAD_PLAYER_DIE)
    if not translated_death_message:
        warn('Failed to translate the death message. Use origin message instead.')
    if isinstance(sender_thread, MessageSenderThread):
        # If the message sender thread is alive.
        sender_thread.push(msg)


def on_player_made_advancement(server, player, advancement):
    if not enabled:
        return
    translated_advancement = translate(language[translating[CFG_TRANSLATION_FROM]],
                                       language[translating[CFG_TRANSLATION_TO]], advancement)
    log('translation: {} -> {}'.format(advancement, translated_advancement))
    if not translated_advancement:
        warn('Failed to translate the advancement name. Use origin name instead.')
    msg = Message.from_server_console_echo(
        '{player_id}达成成就{advancement}'.format(player_id=player,
                                              advancement=translated_advancement if translated_advancement else advancement),
        HEAD_PLAYER_ADVANCEMENT)
    if isinstance(sender_thread, MessageSenderThread):
        # If the message sender thread is alive.
        sender_thread.push(msg)
