const { app, BrowserWindow, ipcMain } = require('electron');
const { SerialPort } = require('serialport');
const path = require('path');

let mainWindow;
let port;

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1280,
        height: 800,
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            preload: path.join(__dirname, 'preload.js')
        }
    });

    // Hard-block Developer Tools so competitors cannot inspect your UI components
    mainWindow.webContents.on('developer-tools-opened', () => {
        mainWindow.webContents.closeDevTools();
    });

    mainWindow.loadFile('KASH_Diagnostics_v2.0.html');
}

// Secure hardware listener
ipcMain.handle('connect-hardware', async () => {
    try {
        // Change 'COM3' to match whatever COM port your USB link adapter uses
        port = new SerialPort({ path: 'COM3', baudRate: 38400 }); 
        return { success: true };
    } catch (err) {
        return { success: false, error: err.message };
    }
});

ipcMain.handle('get-rpm', async () => {
    if (!port) return 0;
    return new Promise((resolve) => {
        port.write('01 0c\r'); // Your hidden proprietary car request hex
        port.once('data', (data) => {
            resolve(data.toString()); // Safely parsed in the backend
        });
    });
});

app.whenReady().then(createWindow);
