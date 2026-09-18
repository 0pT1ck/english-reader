import Foundation
import ERContract

/// 句子池 the sentence pool / 考句 question / 提示 hint
///
/// **句子是服务端生成的内容，怎么分是学习者的事。** 那条线（P9 §2）把两件事分开了:
/// 造句子要钱、要模型，是工厂的活；而「这一句该当考题还是当提示」取决于**你读过
/// 什么**，那是学习记录。所以服务端把一个词的句子**原样全给**，这里来分。
///
/// **分法是算出来的，不是存的**（`verify_phase3` 5.4 守的就是这句）:多读一篇文章，
/// 里面的句子就自动从考题搬到提示，没有要维护的字段。
///
/// 三条规则，逐条镜像服务端 `sentences.split_pools`:
///
/// * **生成的句子永远是考题**，不当提示（`source != "corpus"`）。
/// * 语料里的句子:**出自你读完过的文章** → 提示。
/// * 或者**那一句你确定见过** → 提示。
///
/// 第三条不能省。判据是「你见过这一句吗」，而不是「你读完那篇了吗」——
/// 两者在最普通的情形里就分家:你是在读的时候标记的，所以标记所在那一句肯定见过，
/// **而文章可能几小时后才读完**。少了第三条，那一句算「没见过」、可以被抽成考题，
/// 于是你被五分钟前刚读过的那一句考了——**看着像道容易题，落下来是一个虚高的评级，
/// 而没有任何东西会报告它**。
public enum SentencePool {

    /// 一个词（的一个义项）的全部句子，以及它们分成的两池。
    public struct Split: Sendable, Equatable {
        public var questions: [Components.Schemas.SentenceCard]
        public var hints: [Components.Schemas.SentenceCard]
    }

    /// 按学习者读过什么来分。
    ///
    /// - Parameters:
    ///   - finished: 读完过的文章。
    ///   - seen: 确定见过的句子（标记所在那些）。
    public static func split(_ sentences: [Components.Schemas.SentenceCard],
                             finished: Set<Int>,
                             seen: Set<Int>) -> Split {
        var questions: [Components.Schemas.SentenceCard] = []
        var hints: [Components.Schemas.SentenceCard] = []
        for card in sentences {
            guard card.source == "corpus" else {
                // 生成的句子永远是考题。它们是为了教这个词才写的，
                // 当提示等于把答案当提示给出去。
                questions.append(card)
                continue
            }
            let fromFinished = card.article_id.map { finished.contains($0) } ?? false
            let alreadySeen = card.sentence_id.map { seen.contains($0) } ?? false
            if fromFinished || alreadySeen {
                hints.append(card)
            } else {
                questions.append(card)
            }
        }
        return Split(questions: questions, hints: hints)
    }
}
