export {}

declare global {
  interface SerialPort {
    readonly writable: WritableStream<Uint8Array> | null
    open(options: { baudRate: number }): Promise<void>
    close(): Promise<void>
  }

  interface Serial {
    requestPort(): Promise<SerialPort>
  }

  interface Navigator {
    readonly serial: Serial
  }
}
