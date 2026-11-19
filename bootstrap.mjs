import { createServer } from 'node:net'
createServer(socket => socket.destroy()).listen(10000, '0.0.0.0')
