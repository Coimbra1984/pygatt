from __future__ import print_function

from collections import defaultdict
import logging
import platform
import string
import sys
import time
import threading
try:
    import pexpect
except Exception as e:
    if platform.system() != 'Windows':
        print("WARNING:", e, file=sys.stderr)

from pygatt import constants
from pygatt import exceptions
from pygatt.backends.backend import BLEBackend

log = logging.getLogger(__name__)


class GATTToolBackend(BLEBackend):
    """
    Backend to pygatt that uses gatttool/bluez on the linux command line.
    """
    _GATTTOOL_PROMPT = r".*> "

    def __init__(self, hci_device='hci0', loghandler=None,
                 loglevel=logging.DEBUG, gatttool_logfile=None):
        """
        Initialize.

        hci_device -- the hci_device to use with GATTTool.
        loghandler -- logging.handler object to use for the logger.
        loglevel -- log level for this module's logger.
        """
        # Set up logging
        self._loglock = threading.Lock()
        log.setLevel(loglevel)
        if loghandler is None:
            loghandler = logging.StreamHandler()  # prints to stderr
            formatter = logging.Formatter(
                '%(asctime)s %(name)s %(levelname)s - %(message)s')
            loghandler.setLevel(loglevel)
            loghandler.setFormatter(formatter)
        log.addHandler(loghandler)

        # Internal state
        self._handles = {}
        self._subscribed_handlers = {}
        self._lock = threading.Lock()
        self._connection_lock = threading.RLock()
        self._running = True
        self._callbacks = defaultdict(set)
        self._thread = None  # background notification receiving thread
        self._con = None  # gatttool interactive session

        # Start gatttool interactive session for device
        gatttool_cmd = ' '.join([
            'gatttool',
            '-i',
            hci_device,
            '-I'
        ])
        log.debug('gatttool_cmd=%s', gatttool_cmd)
        self._con = pexpect.spawn(
            gatttool_cmd,
            logfile=gatttool_logfile if gatttool_logfile else None)
        # Wait for response
        self._con.expect(r'\[LE\]>', timeout=1)

        # Start the notification receiving thread
        self._thread = threading.Thread(target=self.run)
        self._thread.daemon = True
        self._thread.start()

    def bond(self):
        """Securely Bonds to the BLE device."""
        log.info('Bonding')
        self._con.sendline('sec-level medium')
        self._con.expect(self._GATTTOOL_PROMPT, timeout=1)

    def connect(self, address, timeout=constants.DEFAULT_CONNECT_TIMEOUT_S):
        """Connect to the device."""
        log.info('Connecting with timeout=%s', timeout)
        self._address = address
        try:
            with self._connection_lock:
                self._con.sendline('connect %s' % self._address)
                self._con.expect(r'Connection successful.*\[LE\]>', timeout)
        except pexpect.TIMEOUT:
            message = ("Timed out connecting to %s after %s seconds."
                       % (self._address, timeout))
            log.error(message)
            raise exceptions.NotConnectedError(message)

    # FIXME: use gatttool char-desc and parse the complete output to correctly
    #        identify the profile structure
    def get_handle(self, uuid, descriptor_uuid=None):
        """
        Look up and return the handle for an attribute by its UUID.
        :param uuid: The UUID of the characteristic.
        :type uuid: str
        :return: None if the UUID was not found.
        """
        if uuid not in self._handles:
            log.debug("Looking up handle for characteristic %s", uuid)
            with self._connection_lock:
                self._con.sendline('characteristics')

                timeout = 2
                while True:
                    try:
                        self._con.expect(
                            r"handle: 0x([a-fA-F0-9]{4}), "
                            "char properties: 0x[a-fA-F0-9]{2}, "
                            "char value handle: 0x[a-fA-F0-9]{4}, "
                            "uuid: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\r\n",  # noqa
                            timeout=timeout)
                    except pexpect.TIMEOUT:
                        break
                    except pexpect.EOF:
                        break
                    else:
                        try:
                            handle = int(self._con.match.group(1), 16)
                            char_uuid = self._con.match.group(2).strip()
                            self._handles[char_uuid] = handle
                            log.debug(
                                "Found characteristic %s, handle: %d",
                                char_uuid,
                                handle)

                            # The characteristics all print at once, so after
                            # waiting 1-2 seconds for them to all fetch, you can
                            # load the rest without much delay at all.
                            timeout = .01
                        except AttributeError:
                            pass

        if len(self._handles) == 0:
            raise exceptions.BluetoothLEError(
                "No characteristics found - disconnected unexpectedly?")

        handle = self._handles.get(uuid)
        if handle is None:
            message = "No characteristic found matching %s" % uuid
            log.warn(message)
            raise exceptions.BluetoothLEError(message)

        log.debug(
            "Characteristic %s, handle: %d", uuid, handle)
        return handle

    def _expect(self, expected,
                timeout=constants.DEFAULT_TIMEOUT_S):
        """
        We may (and often do) get an indication/notification before a
        write completes, and so it can be lost if we "expect()"'d something
        that came after it in the output, e.g.:
        > char-write-req 0x1 0x2
        Notification handle: xxx
        Write completed successfully.
        >
        Anytime we expect something we have to expect noti/indication first for
        a short time.
        """
        with self._connection_lock:
            patterns = [
                expected,
                'Notification handle = .*? \r',
                'Indication   handle = .*? \r',
                '.*Invalid file descriptor.*',
                '.*Disconnected\r',
            ]
            while True:
                try:
                    matched_pattern_index = self._con.expect(patterns, timeout)
                    if matched_pattern_index == 0:
                        break
                    elif matched_pattern_index in [1, 2]:
                        self._handle_notification(self._con.after)
                    elif matched_pattern_index in [3, 4]:
                        message = ""
                        if self._running:
                            message = ("Unexpectedly disconnected - do you "
                                       "need to clear bonds?")
                            log.error(message)
                            self._running = False
                        raise exceptions.NotConnectedError(message)
                except pexpect.TIMEOUT:
                    raise exceptions.NotificationTimeout(
                        "Timed out waiting for a notification")

    # TODO: this might be better named attribute write since it can write to
    #       characteristics, descriptors, or any other writable attribute with
    #       a valid handle
    def char_write(self, handle, value, wait_for_response=False):
        """
        Writes a value to a given characteristic handle.
        :param handle:
        :param value:
        :param wait_for_response:
        """
        with self._connection_lock:
            hexstring = ''.join('%02x' % byte for byte in value)

            # FIXME this is not actually how it works
            # The "write" handle as numbered in gatttol is the base handle
            # number + 1 - this may or may not be gatttool/BlueZ specific. IF it
            # is, this logic can be moved up 1 level.
            handle += 1

            if wait_for_response:
                cmd = 'req'
            else:
                cmd = 'cmd'

            cmd = 'char-write-%s 0x%02x %s' % (cmd, handle, hexstring)

            log.debug('Sending cmd=%s', cmd)
            self._con.sendline(cmd)

            if wait_for_response:
                try:
                    self._expect('Characteristic value written successfully')
                except exceptions.NoResponseError:
                    log.error("No response received", exc_info=True)
                    raise

            log.info('Sent cmd=%s', cmd)

    def char_read_uuid(self, uuid):
        """
        Reads a Characteristic by UUID.
        :param uuid: UUID of Characteristic to read.
        :type uuid: str
        :return: bytearray of result.
        :rtype: bytearray
        """
        with self._connection_lock:
            self._con.sendline('char-read-uuid %s' % uuid)
            self._expect('value: .*? \r')

            rval = self._con.after.split()[1:]

            return bytearray([int(x, 16) for x in rval])

    # TODO: this might be better named attribute read since it can read from
    #       characteristics, descriptors, or any other readable attribute with
    #       a valid handle
    def char_read_hnd(self, handle):
        """
        Reads a Characteristic by Handle.
        :param handle: Handle of Characteristic to read.
        :type handle: str
        :return: bytearray of result
        :rtype: bytearray
        """
        with self._connection_lock:
            self._con.sendline('char-read-hnd 0x%02x' % handle)
            self._expect('descriptor: .*?\r')

            rval = self._con.after.split()[1:]

            return bytearray([int(n, 16) for n in rval])

    # TODO: this might be better structured by passing in a characteristic
    #       object and having the method determine if it can be subscribed to
    #       and then doing the subscribing. See the "bluetooth-adapter" branch
    #       for an example of how this looks with the bgapi backend
    def subscribe(self, uuid, callback=None, indication=False):
        """
        Enables subscription to a Characteristic with ability to call callback.
        :param uuid:
        :param callback:
        :param indication:
        :return:
        :rtype:
        """
        log.info(
            'Subscribing to uuid=%s with callback=%s and indication=%s',
            uuid, callback, indication)
        definition_handle = self.get_handle(uuid)

        # FIXME These assumptions are sometimes wrong
        # Expect notifications on the value handle...
        value_handle = definition_handle + 1
        # but write to the characteristic config to enable notifications
        characteristic_config_handle = value_handle + 1

        if indication:
            properties = bytearray([0x02, 0x00])
        else:
            properties = bytearray([0x01, 0x00])

        try:
            self._lock.acquire()

            if callback is not None:
                self._callbacks[value_handle].add(callback)

            if self._subscribed_handlers.get(value_handle, None) != properties:
                self.char_write(
                    characteristic_config_handle,
                    properties,
                    wait_for_response=False
                )
                log.debug("Subscribed to uuid=%s", uuid)
                self._subscribed_handlers[value_handle] = properties
            else:
                log.debug("Already subscribed to uuid=%s", uuid)
        finally:
            self._lock.release()

    def _handle_notification(self, msg):
        """
        Receive a notification from the connected device and propagate the value
        to all registered callbacks.
        """
        hex_handle, _, hex_value = string.split(msg.strip(), maxsplit=5)[3:]
        handle = int(hex_handle, 16)
        value = bytearray.fromhex(hex_value)

        log.info('Received notification on handle=%s, value=%s',
                 hex_handle, hex_value)
        try:
            self._lock.acquire()

            if handle in self._callbacks:
                for callback in self._callbacks[handle]:
                    callback(handle, value)
        finally:
            self._lock.release()

    def stop(self):
        """
        Stop the backgroud notification handler in preparation for a
        disconnect.
        """
        log.info('Stopping')
        self._running = False

        if self._con.isalive():
            self._con.sendline('exit')
            while True:
                if not self._con.isalive():
                    break
                time.sleep(0.1)
            self._con.close()

    def run(self):
        """
        Run a background thread to listen for notifications.
        """
        log.info('Running...')
        while self._running:
            with self._connection_lock:
                try:
                    self._expect("fooooooo", timeout=.1)
                except exceptions.NotificationTimeout:
                    pass
                except (exceptions.NotConnectedError, pexpect.EOF):
                    break
            # TODO need some delay to avoid aggresively grabbing the lock,
            # blocking out the others. worst case is 1 second delay for async
            # not received as a part of another request
            time.sleep(.01)
        log.info("Listener thread finished")