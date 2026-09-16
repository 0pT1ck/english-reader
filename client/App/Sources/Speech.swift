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

    /// 口音与语速从偏好来（P8 决定 18、19）。
    ///
    /// 原先两样都写死：`en-US`，以及 `× 0.9`——那个 0.9 的理由是「单词单独念
    /// 的时候默认语速偏快，听不清词尾」。**那是为单个词定的**，整句朗读时它偏慢，
    /// 而四六级听力英音美音都考，所以两样都该由人自己定。
    func say(_ text: String, voice: String = "en-US", rate: Double = 0.9) {
        guard !text.isEmpty else { return }
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: voice)
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * Float(rate)
        synthesizer.speak(utterance)
    }
}
