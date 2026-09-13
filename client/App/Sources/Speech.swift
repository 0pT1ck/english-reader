import AVFoundation
import Observation

/// 朗读（决定 27）。
///
/// 用系统自带的离线语音合成：**不联网、不花钱、不用后端出力**。功能本身在草图里
/// 只是个图标，做出来的代价小到没有理由不做。
///
/// 合成器要活得比一次调用长——局部变量建出来的那个会在说完之前被回收，
/// 于是「点了没声音」。这类错不报任何东西，所以它被留在这条注释里。
@MainActor
@Observable
final class Speaker {
    private let synthesizer = AVSpeechSynthesizer()

    func say(_ text: String) {
        guard !text.isEmpty else { return }
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        // 单词单独念的时候，默认语速偏快，听不清词尾。
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * 0.9
        synthesizer.speak(utterance)
    }
}
