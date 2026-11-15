// Raw native-rate signal in 1-second blocks, as the server stores it
// (src/server/raw_signals.py): shared by the Muse headband (muse-eeg.js)
// and the Empatica E4 wristband (e4-wristband.js).

// Marks a lost sample in a raw block (raw_signals.MISSING / MISSING_32).
export const RAW_MISSING = -32768
export const RAW_MISSING_32 = -2147483648

// Collects every channel of one stream into 1-second blocks by sample
// index: onBlock(firstIndex, Int16Array, or Int32Array with int32) with the
// counts interleaved by sample (s0c0, s0c1, ..., s1c0, ...). A block goes
// once every channel has filled it, or with the missing value in the gaps
// once two newer blocks have started (a lost packet), so every second that
// saw any data is kept and no sample shifts.
export class RawBlockAssembler {
    constructor(onBlock, { channels, size, int32 = false }) {
        this.onBlock = onBlock
        this.channels = channels
        this.size = size
        this.ArrayType = int32 ? Int32Array : Int16Array
        this.missing = int32 ? RAW_MISSING_32 : RAW_MISSING
        this.open = new Map()
    }

    push(channel, firstIndex, counts) {
        const per = this.size * this.channels
        for (let k = 0; k < counts.length; k++) {
            const idx = firstIndex + k
            const b = Math.floor(idx / this.size)
            let block = this.open.get(b)
            if (!block) {
                block = { data: new this.ArrayType(per).fill(this.missing), filled: 0 }
                this.open.set(b, block)
            }
            block.data[(idx - b * this.size) * this.channels + channel] = counts[k]
            block.filled++
            if (block.filled === per) {
                this.open.delete(b)
                this.onBlock(b * this.size, block.data)
            }
        }
        const newest = Math.floor((firstIndex + counts.length - 1) / this.size)
        for (const [b, block] of [...this.open.entries()].sort((x, y) => x[0] - y[0])) {
            if (b >= newest - 1) break
            this.open.delete(b)
            this.onBlock(b * this.size, block.data)
        }
    }
}

// Int16Array or Int32Array -> base64 of its little-endian bytes (what the
// server decodes).
export const encodeBlock = (data) => {
    const width = data.BYTES_PER_ELEMENT
    const bytes = new Uint8Array(data.length * width)
    const view = new DataView(bytes.buffer)
    data.forEach((v, i) => (width === 4 ? view.setInt32(i * 4, v, true) : view.setInt16(i * 2, v, true)))
    let text = ""
    for (let i = 0; i < bytes.length; i += 0x8000) {
        text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000))
    }
    return btoa(text)
}
