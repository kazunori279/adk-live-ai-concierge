/**
 * Audio Player Worklet
 *
 * The graph is worklet → gain → destination, and the gain is the whole of what
 * the speaker control can reach: a page cannot touch the Pi's mixer, so this
 * node is the only place between the model's samples and the panel's speaker
 * where a volume exists at all. It sits after the worklet and before the
 * destination, and nothing between the two measures the signal — no analyser,
 * no meter — so turning it down changes what is heard and nothing else.
 *
 * `gain` is passed in rather than set afterwards. The node is in the graph
 * before the first packet arrives, and a terminal somebody muted must not let
 * the front of the first word out at full volume while a ramp catches up.
 */
export async function startAudioPlayerWorklet(gain = 1) {
    const audioContext = new AudioContext({
        sampleRate: 24000
    });

    const workletURL = new URL('./pcm-player-processor.js', import.meta.url);
    await audioContext.audioWorklet.addModule(workletURL);

    const audioPlayerNode = new AudioWorkletNode(audioContext, 'pcm-player-processor');
    const gainNode = new GainNode(audioContext, { gain });
    audioPlayerNode.connect(gainNode).connect(audioContext.destination);

    return [audioPlayerNode, audioContext, gainNode];
}
