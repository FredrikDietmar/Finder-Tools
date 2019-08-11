from UM.Application import Application
from UM.Logger import Logger
from UM.OutputDevice.OutputDevice import OutputDevice
from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin
from UM.Extension import Extension

from PyQt5.QtWidgets import QDialog, QPushButton, QVBoxLayout, QProgressBar, QLineEdit, QLabel
from PyQt5.QtCore import Qt

import socket
import time
import zlib
from threading import Thread

class FinderToolsSettings(Extension):
    def __init__(self):
        super().__init__()

        self.setMenuName("Finder Tools")
        self.addMenuItem("IP address", self.openSetIpDialog)

        #Create IP adress dialog
        self.ipDialog = QDialog()
        self.ipDialog.setWindowTitle('IP address')
        self.ipDialog.accepted.connect(self.ipDialogClosed)
        self.ipDialog.rejected.connect(self.ipDialogClosed)
        l = QVBoxLayout(self.ipDialog)
        self.ipLineEdit = QLineEdit(self.ipDialog)
        self.ipLineEdit.setPlaceholderText('0.0.0.0')
        l.addWidget(self.ipLineEdit)
        b = QPushButton("Ok")
        b.clicked.connect(self.ipDialog.accept)
        l.addWidget(b)
        self.ipDialog.setLayout(l)

        #Read preferences
        self._preferences = Application.getInstance().getPreferences()
        if not self._preferences.getValue('FinderTools/ip_address'):
            self._preferences.addPreference('FinderTools/ip_address', '')
        else:
            self.ipLineEdit.setText(self._preferences.getValue('FinderTools/ip_address'))

    def openSetIpDialog(self):
        self.ipDialog.open()
        self.ipDialog.activateWindow()
    
    def ipDialogClosed(self):
        if self.ipDialog.result() == QDialog.Accepted:
            self._preferences.setValue('FinderTools/ip_address', self.ipLineEdit.text())
        else:
            self.ipLineEdit.setText(self._preferences.getValue('FinderTools/ip_address'))

    
class SendToFinderPlugin(OutputDevicePlugin):
    def start(self):
        self.getOutputDeviceManager().addOutputDevice(SendToFinder())

    def stop(self):
        self.getOutputDeviceManager().removeOutputDevice('send_to_finder')

class SendToFinder(OutputDevice):
    def __init__(self):
        super().__init__('send_to_finder')

        self.setName('Send to Finder')
        self.setShortDescription('Send to Finder')
        self.setDescription('Send to Finder')

        self.fileBytes = b''
        self.fileName = ''
        self.transfering = False

        #Create progress window
        self.progressWindow = QDialog()
        l = QVBoxLayout(self.progressWindow)
        self.progressMsg = QLabel('')
        self.progressMsg.setWordWrap(True)
        l.addWidget(self.progressMsg)
        self.progressBar = QProgressBar()
        l.addWidget(self.progressBar)
        b = QPushButton('Abort')
        b.clicked.connect(self.abortTransfer)
        l.addWidget(b)

    def abortTransfer(self):
        self.abort = True
        if self.transfering:
            self.progressMsg.setText('Aborting...')
        else:
            self.progressWindow.reject()
        

    def requestWrite(self, nodes, file_name = None, limit_mimetypes = None, file_handler = None, **kwargs):
        if not file_name or self.transfering:
            return False

        #Get g-code
        active_build_plate = Application.getInstance().getMultiBuildPlateModel().activeBuildPlate
        scene = Application.getInstance().getController().getScene()

        gcode_dict = getattr(scene, "gcode_dict")
        gcode_list = gcode_dict.get(active_build_plate, None)

        toSend = ''        
        if gcode_list:
            for gcodes in gcode_list:
                for gcode in gcodes.split('\n'):
                    if not gcode or gcode[0] == ';':
                        continue
                    toSend += gcode + '\n'
        
        self.fileBytes = toSend.encode('utf-8')
        self.fileName = file_name

        #Start transfer
        self.progressMsg.setText('Connecting...')
        self.transferThread = Thread(target=self._sendFile)
        self.transferThread.start()

        self.progressBar.setValue(0)
        self.progressWindow.exec_()

        return True
    
    def _sendAndRecv(self, s, msg):
        try:
            s.sendall(msg)
            res = s.recv(128)
        except Exception as e:
            Logger.log('e', e)
            return ''

        if res:
            return res.decode('utf-8')
        else:
            return ''

    def _sendFile(self):
        ipAddr = Application.getInstance().getPreferences().getValue("FinderTools/ip_address")
        self.transfering = True
        self.abort = False

        with socket.socket() as s:
            #Connect
            try:
                s.settimeout(10.0)
                s.connect((ipAddr, 8899))
            except (socket.gaierror, socket.herror) as e:
                Logger.log('e', e)
                self.progressMsg.setText('Connection failed: Check IP address.')
                self.transfering = False
                return
            except socket.timeout as e:
                Logger.log('e', e)
                self.progressMsg.setText('Connection failed: Timeout.')
                self.transfering = False
                return
            except Exception as e:
                Logger.log('e', e)
                self.progressMsg.setText('Connection failed.')
                self.transfering = False
                return

            self.progressMsg.setText('Transfering...')

            #Init
            res = self._sendAndRecv(s, '~M601 S1\r\n'.encode('utf-8'))

            if res:
                if not res.startswith('CMD M601 Received.\r\nControl Success.'):
                    Logger.log('e', res)
                    self.progressMsg.setText('Printer initialization failed.')
                    self.transfering = False
                    return
            else:
                self.progressMsg.setText('Printer initialization error.')
                self.transfering = False
                return

            #Start upload
            res = self._sendAndRecv(s, '~M650\r\n'.encode('utf-8'))
            if res:
                if not res.startswith('CMD M650 Received.\r\nX:'):
                    Logger.log('e', res)
                    self.progressMsg.setText('Start upload failed.')
                    self.transfering = False
                    return
            else:
                self.progressMsg.setText('Start upload error.')
                self.transfering = False
                return

            fileLength = len(self.fileBytes)

            #Set filename
            res = self._sendAndRecv(s, ('~M28 ' + str(fileLength) + ' 0:/user/' + self.fileName[:32] + '.g\r\n').encode('utf-8'))
            if res:
                if not res.startswith('CMD M28 Received.\r\nWriting to file:'):
                    Logger.log('e', res)
                    self.progressMsg.setText('Create file failed.')
                    self.transfering = False
                    return
            else:
                self.progressMsg.setText('Create file error.')
                self.transfering = False
                return

            #Send file
            startByte = 0
            bytesRead = 4096
            count = 0

            while bytesRead == 4096 and not self.abort:
                data = self.fileBytes[startByte:startByte+4096]
                bytesRead = len(data)
                if bytesRead > 0:
                    header = b'\x5a\x5a\xa5\xa5' # 4 constant bytes
                    header += count.to_bytes(4, byteorder='big') # Part number
                    header += bytesRead.to_bytes(4, byteorder='big') # Part length
                    header += zlib.crc32(data).to_bytes(4, byteorder='big') # CRC32 for part

                    if bytesRead < 4096:
                        data = data + b'\x00'*(4096-bytesRead) # Pad with 0-bytes

                    data = header + data
                    res = self._sendAndRecv(s, data)

                    if res:
                        if 'error' in res:
                            Logger.log('e', res)
                            self.progressMsg.setText('Part {} failed.'.format(count))
                            self.transfering = False
                            return
                    else:
                        self.progressMsg.setText('Part {} error.'.format(count))
                        self.transfering = False
                        return

                    count += 1
                    startByte += 4096
                    self.progressBar.setValue(100*startByte/fileLength)

            if not self.abort:
                time.sleep(1)
                res = self._sendAndRecv(s, '~M29\r\n'.encode('utf-8')) #End file transfer
                if res:
                    if not res.startswith('CMD M29 Received.\r\nDone saving file.'):
                        Logger.log('e', res)
                        self.progressMsg.setText('Saving file failed.')
                        self.transfering = False
                        return
                else:
                    self.progressMsg.setText('Saving file error.')
                    self.transfering = False
                    return

                res = self._sendAndRecv(s, ('~M23 0:/user/' + self.fileName[:32] + '.g\r\n').encode('utf-8')) #Start print
                if res:
                    if not res.startswith('CMD M23 Received.\r\nFile opened:'):
                        Logger.log('e', res)
                        self.progressMsg.setText('Start print failed. Try to start it manually.')
                        self.transfering = False
                        return
                else:
                    self.progressMsg.setText('Start print error. Try to start it manually.')
                    self.transfering = False
                    return
                
                self.progressWindow.accept()
            else:
                self.progressWindow.reject()
                
        self.transfering = False
