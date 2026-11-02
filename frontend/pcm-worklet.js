// Runs on the audio thread: forwards each 128-sample block of microphone audio to the page.
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true; // keep running
  }
}
registerProcessor("pcm-capture", PcmCapture);
