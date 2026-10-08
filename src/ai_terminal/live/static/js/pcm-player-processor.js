/**
 * PCM Player AudioWorklet Processor
 */
const RAMP_FRAMES = 128;

class PCMPlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();

    this.bufferSize = 24000 * 180;
    this.buffer = new Float32Array(this.bufferSize);
    this.writeIndex = 0;
    this.readIndex = 0;

    this.lastSample = 0.0;
    this.rampUpStep = 0;
    this.rampDownSource = 0.0;
    this.rampDownStep = RAMP_FRAMES;
    this.isRampingDown = false;

    this.port.onmessage = (event) => {
      if (event.data.command === 'endOfAudio') {
        this.readIndex = this.writeIndex;
        if (this.lastSample !== 0.0) {
          this.isRampingDown = true;
          this.rampDownSource = this.lastSample;
          this.rampDownStep = 0;
        }
        this.rampUpStep = 0;
        return;
      }
      const int16Samples = new Int16Array(event.data);
      this._enqueue(int16Samples);
    };
  }

  _enqueue(int16Samples) {
    for (let i = 0; i < int16Samples.length; i++) {
      const floatVal = int16Samples[i] / 32768;
      this.buffer[this.writeIndex] = floatVal;
      this.writeIndex = (this.writeIndex + 1) % this.bufferSize;
      if (this.writeIndex === this.readIndex) {
        this.readIndex = (this.readIndex + 1) % this.bufferSize;
      }
    }
  }

  process(inputs, outputs, parameters) {
    const output = outputs[0];
    const framesPerBlock = output[0].length;
    for (let frame = 0; frame < framesPerBlock; frame++) {
      let sample = 0.0;
      if (this.readIndex !== this.writeIndex) {
        const rawSample = this.buffer[this.readIndex];
        this.readIndex = (this.readIndex + 1) % this.bufferSize;

        if (this.rampUpStep < RAMP_FRAMES) {
          this.rampUpStep++;
          sample = rawSample * (this.rampUpStep / RAMP_FRAMES);
        } else {
          sample = rawSample;
        }

        this.lastSample = sample;
        this.isRampingDown = false;
      } else {
        if (!this.isRampingDown) {
          if (this.lastSample !== 0.0) {
            this.isRampingDown = true;
            this.rampDownSource = this.lastSample;
            this.rampDownStep = 0;
          }
          this.rampUpStep = 0;
        }

        if (this.isRampingDown) {
          this.rampDownStep++;
          const factor = 1.0 - (this.rampDownStep / RAMP_FRAMES);
          sample = this.rampDownSource * factor;
          this.lastSample = sample;
          if (this.rampDownStep >= RAMP_FRAMES) {
            this.isRampingDown = false;
            this.lastSample = 0.0;
            sample = 0.0;
          }
        } else {
          sample = 0.0;
          this.lastSample = 0.0;
        }
      }

      output[0][frame] = sample;
      if (output.length > 1) {
        output[1][frame] = sample;
      }
    }
    return true;
  }
}

registerProcessor('pcm-player-processor', PCMPlayerProcessor);
