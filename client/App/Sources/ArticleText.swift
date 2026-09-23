import SwiftUI
import UIKit
import ERCore
import ERContract

extension NSAttributedString.Key {
    /// 每个词在正文里带着自己的序号，点下去就能问「点到的是第几个 token」。
    /// 比反过来（记下每个词的坐标再比对触点）少一整套要维护的状态。
    static let tokenSeq = NSAttributedString.Key("erTokenSeq")
}

/// 正文里的一个词。`range` 是在**这一段**里的位置，不是全文。
struct ReaderToken: Equatable {
    let seq: Int
    let headword: String?
    /// UTF-16 偏移，因为 `NSAttributedString` 用的就是它。
    let location: Int
    let length: Int
}

/// 一个自然段。
///
/// **为什么按段落切，不按句子。**一句一块的话，每句都会另起一行——那不是文章，
/// 是句子列表。按段落切之后还剩一个好处：段落是能锚定的东西，所以
/// 「跳回上次位置」和「读到第几句」都有地方挂（决定 15）。
struct ReaderParagraph: Identifiable, Equatable {
    let id: Int
    let text: String
    let tokens: [ReaderToken]
    /// 这一段的第一句是全文第几句。`article.progress` 报的就是它。
    let firstSentenceSeq: Int
}

enum ArticleLayout {
    /// 把正文切成段，并把每个 token 归到它所在的那一段。
    ///
    /// **服务端给的 `char_start` 是 Python 的字符下标**，也就是 Unicode 码位。
    /// Swift 的 `Character` 是字形簇，两者在英文上一致、在别的地方不一定，
    /// 所以这里一律走 `unicodeScalars`——不是讲究，是这批下标必须对得上，
    /// 对不上就是词高亮错位，而且不会报错。
    static func paragraphs(
        body: String,
        tokens: [Components.Schemas.Token],
        sentences: [Components.Schemas.Sentence]
    ) -> [ReaderParagraph] {
        let scalars = Array(body.unicodeScalars)
        var result: [ReaderParagraph] = []
        var index = 0
        var paragraphIndex = 0

        while index < scalars.count {
            // 跳过空白与换行，段落从第一个实字符开始。
            while index < scalars.count, scalars[index] == "\n" || scalars[index] == "\r" {
                index += 1
            }
            guard index < scalars.count else { break }

            let start = index
            while index < scalars.count, scalars[index] != "\n", scalars[index] != "\r" {
                index += 1
            }
            let end = index

            var view = String.UnicodeScalarView()
            view.append(contentsOf: scalars[start..<end])
            let text = String(view)
            guard !text.trimmingCharacters(in: .whitespaces).isEmpty else { continue }

            // 码位下标 → UTF-16 偏移。英文里两者一样，但「一样」不是能假设的东西。
            var utf16Offsets: [Int] = []
            utf16Offsets.reserveCapacity(end - start + 1)
            var running = 0
            for scalar in scalars[start..<end] {
                utf16Offsets.append(running)
                running += UTF16.width(scalar)
            }
            utf16Offsets.append(running)

            let inside = tokens.filter { $0.char_start >= start && $0.char_start < end }
            let built: [ReaderToken] = inside.compactMap { token in
                let from = token.char_start - start
                let to = min(token.char_end - start, end - start)
                guard from >= 0, to > from, to < utf16Offsets.count else { return nil }
                return ReaderToken(
                    seq: token.seq,
                    headword: token.headword,
                    location: utf16Offsets[from],
                    length: utf16Offsets[to] - utf16Offsets[from]
                )
            }

            let sentenceSeq = sentences.first { $0.char_end > start }?.seq
                ?? sentences.first?.seq ?? 0

            result.append(ReaderParagraph(
                id: paragraphIndex, text: text, tokens: built,
                firstSentenceSeq: sentenceSeq))
            paragraphIndex += 1
        }
        return result
    }
}

/// 一段正文，画成可点的文字。
///
/// 用 `UITextView` 而不是 SwiftUI 的 `Text`：要的是**按字符命中**（点哪个词就选
/// 哪个词）和真正的排版断行，这两样 `Text` 都给不了。滚动交给外面的
/// `ScrollView`，这里只负责一段。
struct ParagraphTextView: UIViewRepresentable {
    let paragraph: ReaderParagraph
    /// 要画记号的段。**只画自己标过的**（决定 11、12）：
    /// 目标词、超纲词、派生词一律不画——满屏记号之后文章就不是文章了。
    /// 一段可以是一个词，也可以是一整个词组（P12）。
    let marks: [MarkSpan]
    /// 正在被点开的那一段，整段底色——点词组的任一半，亮的是整个词组。
    let selected: ClosedRange<Int>?
    /// 字号倍数（P8 决定 17）。**入口在阅读屏的 `···` 里**——调字号要看着正文
    /// 调，在设置页拖一个滑块再退出去看效果，是把两秒的动作做成一分钟。
    ///
    /// 乘在 `UIFontMetrics` 之上，不是取代它：系统的动态字体照旧生效，
    /// 这个倍数只是在它之上再缩放一次。
    var scale: Double = 1.0
    let onTap: (Int) -> Void

    func makeUIView(context: Context) -> UITextView {
        let view = UITextView()
        view.isEditable = false
        view.isScrollEnabled = false
        view.isSelectable = false          // 选中会抢走点词的手势
        view.backgroundColor = .clear
        view.textContainerInset = .zero
        view.textContainer.lineFragmentPadding = 0
        view.adjustsFontForContentSizeCategory = true
        view.setContentCompressionResistancePriority(.required, for: .vertical)

        let tap = UITapGestureRecognizer(
            target: context.coordinator,
            action: #selector(Coordinator.handleTap(_:)))
        view.addGestureRecognizer(tap)
        context.coordinator.onTap = onTap
        return view
    }

    func updateUIView(_ view: UITextView, context: Context) {
        context.coordinator.onTap = onTap
        view.attributedText = attributed()
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: UITextView,
                      context: Context) -> CGSize? {
        guard let width = proposal.width else { return nil }
        let size = uiView.sizeThatFits(
            CGSize(width: width, height: .greatestFiniteMagnitude))
        return CGSize(width: width, height: ceil(size.height))
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator: NSObject {
        var onTap: ((Int) -> Void)?

        @objc func handleTap(_ gesture: UITapGestureRecognizer) {
            guard let view = gesture.view as? UITextView,
                  let text = view.attributedText, text.length > 0 else { return }
            let point = gesture.location(in: view)
            // `closestPosition` 走的是 UITextInput，TextKit 1 还是 2 都答得出来；
            // `layoutManager` 那条路在 TextKit 2 下会是 nil。
            guard let position = view.closestPosition(to: point) else { return }
            let offset = view.offset(from: view.beginningOfDocument, to: position)
            guard offset >= 0, offset < text.length else { return }
            guard let seq = text.attribute(.tokenSeq, at: offset,
                                           effectiveRange: nil) as? Int else { return }
            onTap?(seq)
        }
    }

    private func attributed() -> NSAttributedString {
        let font = UIFontMetrics(forTextStyle: .body)
            .scaledFont(for: .systemFont(ofSize: 19 * CGFloat(scale), weight: .regular))
        let style = NSMutableParagraphStyle()
        style.lineHeightMultiple = 1.32
        style.paragraphSpacing = 0

        let result = NSMutableAttributedString(
            string: paragraph.text,
            attributes: [.font: font,
                         .foregroundColor: UIColor.label,
                         .paragraphStyle: style])

        for token in paragraph.tokens {
            let range = NSRange(location: token.location, length: token.length)
            guard NSMaxRange(range) <= result.length else { continue }
            result.addAttribute(.tokenSeq, value: token.seq, range: range)
        }

        for span in marks {
            guard let range = range(of: span.seqs, length: result.length) else { continue }
            // 两档（不认识 / 模糊）暂时画成同一种（决定 12）；词组也是这一种
            // （P12 决定 ③，美术以后再定）。
            result.addAttributes([
                .underlineStyle: NSUnderlineStyle.single.rawValue
                    | NSUnderlineStyle.patternDash.rawValue,
                .underlineColor: UIColor.secondaryLabel,
            ], range: range)
        }
        if let selected, let range = range(of: selected, length: result.length) {
            result.addAttribute(.backgroundColor,
                                value: UIColor.secondarySystemFill, range: range)
        }
        return result
    }

    /// 一段 token 在这一段正文里占的范围：**从第一个词的开头到最后一个词的结尾**，
    /// 中间的空格一起算进去——跨 Phase 不变量，词组是一个整体。不在这一段的返回 nil。
    private func range(of seqs: ClosedRange<Int>, length: Int) -> NSRange? {
        let inside = paragraph.tokens.filter { seqs.contains($0.seq) }
        guard let first = inside.min(by: { $0.location < $1.location }),
              let last = inside.max(by: { $0.location + $0.length < $1.location + $1.length })
        else { return nil }
        let range = NSRange(location: first.location,
                            length: last.location + last.length - first.location)
        return NSMaxRange(range) <= length ? range : nil
    }
}
