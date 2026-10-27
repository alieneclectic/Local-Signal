// Keep a signed, identifiable app responsible for the Python collector's access.
import Foundation
import AppKit
import Dispatch
import Darwin

let support = Bundle.main.bundleURL.deletingLastPathComponent()
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
application.finishLaunching()
// A connected UDP socket requests local-network access without sending data.
let permissionSocket = socket(AF_INET, SOCK_DGRAM, 0)
if permissionSocket >= 0 {
    var address = sockaddr_in()
    address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    address.sin_family = sa_family_t(AF_INET)
    address.sin_port = in_port_t(5353).bigEndian
    address.sin_addr = in_addr(s_addr: inet_addr("224.0.0.251"))
    withUnsafePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            _ = connect(permissionSocket, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    close(permissionSocket)
}
let child = Process()
child.executableURL = support.appendingPathComponent(".venv/bin/python")
child.arguments = ["-u", support.appendingPathComponent("runtime/server.py").path,
                   "--host", "0.0.0.0", "--data",
                   support.appendingPathComponent("data/hearth.sqlite3").path, "--background"]
child.currentDirectoryURL = support.appendingPathComponent("runtime")
signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let termination = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
termination.setEventHandler { if child.isRunning { child.terminate() } }
termination.resume()
let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
interrupt.setEventHandler { if child.isRunning { child.interrupt() } }
interrupt.resume()
do {
    child.terminationHandler = { process in exit(process.terminationStatus) }
    try child.run()
    application.run()
} catch {
    fputs("Hearth could not start its collector: \(error)\n", stderr)
    exit(1)
}
