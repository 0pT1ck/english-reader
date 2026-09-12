import Foundation
import ERContract
import ERCore

/// Drawing an article in a terminal.
///
/// **This is where 决定 17 earns its keep.** The core says what each word *is*;
/// this file decides that a target word is printed in blue and an out-of-range
/// one in dim grey with a `~`. The phone client will answer the same question
/// differently and neither answer is in the core — which is the point, because
/// a terminal cannot draw a dashed underline at all.
///
/// **The marks are not decoration.** Reading them back is how the day's work is
/// checked: if a phrase does not light up as one thing, or a word inside it
/// shows its own gloss, the failure is visible here rather than in a UI nobody
/// has written yet.
enum Ink {
    /// Swift 6 checks global mutable state across concurrency domains, and this
    /// is genuinely shared. It is set once before anything runs and only read
    /// afterwards, which is what `nonisolated(unsafe)` is for — the alternative
    /// is threading a colour flag through every print in the program.
    nonisolated(unsafe) static var enabled = true

    static func wrap(_ text: String, _ code: String) -> String {
        enabled ? "\u{1B}[\(code)m\(text)\u{1B}[0m" : text
    }

    static func dim(_ t: String) -> String { wrap(t, "2") }
    static func bold(_ t: String) -> String { wrap(t, "1") }
    static func blue(_ t: String) -> String { wrap(t, "34") }
    static func green(_ t: String) -> String { wrap(t, "32") }
    static func red(_ t: String) -> String { wrap(t, "31") }
    static func yellow(_ t: String) -> String { wrap(t, "33") }
    static func magenta(_ t: String) -> String { wrap(t, "35") }
    static func underline(_ t: String) -> String { wrap(t, "4") }
    static func reverse(_ t: String) -> String { wrap(t, "7") }
}

struct ArticleRenderer {
    let article: Components.Schemas.ArticleResponse
    let display: ArticleDisplay

    init(_ article: Components.Schemas.ArticleResponse) {
        self.article = article
        self.display = ArticleDisplay(article: article)
    }

    /// The legend, printed above the text because a reader who does not know
    /// what the colours mean is reading plain text with noise in it.
    static func legend() -> String {
        [
            Ink.blue("蓝色") + "=目标词",
            Ink.green("绿色") + "=派生词",
            Ink.dim("灰色~") + "=超纲词",
            Ink.magenta("紫色") + "=专有名词",
            Ink.red("红底") + "=标了不认识",
            Ink.yellow("黄底") + "=标了模糊",
            Ink.underline("下划线") + "=词组（整体）",
        ].joined(separator: "  ")
    }

    /// The article with every word numbered, because looking a word up in a
    /// terminal means naming it and the surface form is ambiguous — `the`
    /// appears thirty times.
    func body() -> String {
        var lines: [String] = []
        var current = ""
        var lastSentence = -1

        for (index, token) in (article.tokens ?? []).enumerated() {
            let display = self.display.tokens[index]
            if token.sentence_seq != lastSentence, !current.isEmpty {
                lines.append(current)
                current = ""
            }
            lastSentence = token.sentence_seq

            var text = token.surface
            switch display.role {
            case .target: text = Ink.blue(text)
            case .derived: text = Ink.green(text)
            case .beyondSyllabus: text = Ink.dim(text + "~")
            case .properNoun: text = Ink.magenta(text)
            case .plain: break
            }
            switch display.mark {
            case .unknown?: text = Ink.red(text)
            case .fuzzy?: text = Ink.yellow(text)
            case nil: break
            }
            // A phrase is one thing, so it is drawn as one — the underline runs
            // across both words and the space between them (跨 Phase 不变量).
            if display.inPhrase { text = Ink.underline(text) }

            current += Ink.dim("⟨\(index)⟩") + text + " "
        }
        if !current.isEmpty { lines.append(current) }
        return lines.joined(separator: "\n")
    }

    /// What tapping token `seq` shows.
    ///
    /// The branch that matters is the phrase one: the token's own sense was
    /// annotated without knowing it was inside a phrase, so showing the word's
    /// gloss here would be giving the wrong meaning rather than merely an
    /// unhelpful one.
    func panel(at seq: Int) -> String {
        switch display.target(at: seq) {
        case nil:
            return Ink.dim("⟨\(seq)⟩ 没有可查的东西（标点，或者超出范围）")

        case .phrase(let phrase):
            let row = (article.phrases ?? []).first { $0.phrase == phrase.phrase }
            var out = [Ink.bold("词组：\(phrase.phrase)")]
            out.append(Ink.dim("  这是一个整体——点它的任何一半都到这里"))
            if let translation = row?.translation { out.append("  \(translation)") }
            if let definition = row?.definition { out.append(Ink.dim("  \(definition)")) }
            if let mark = phrase.mark { out.append(Ink.red("  已标记：\(mark.rawValue)")) }
            if let state = row?.state {
                out.append(Ink.dim("  词池：\(state.pool)，遇见 \(state.encounters) 次"))
            }
            return out.joined(separator: "\n")

        case .word(let headword, let token):
            guard let entry = article.glossary?.additionalProperties[headword] else {
                return Ink.bold(headword) + Ink.dim("（这篇里没有它的释义）")
            }
            var out = [Ink.bold(headword) + " " + Ink.dim(entry.phonetic ?? "")]

            switch token.role {
            case .beyondSyllabus:
                out.append(Ink.dim("  超纲词——暂时不用学会它"))
            case .properNoun:
                out.append(Ink.dim("  专有名词，可以跳过"))
            case .derived:
                if let derivation = entry.derivation {
                    out.append("  构词：" + (derivation.breakdown_zh ?? derivation.root))
                    out.append(Ink.dim("  词根 \(derivation.root)"
                        + (derivation.affix.map { " + \($0)" } ?? "")))
                }
            case .target, .plain:
                break
            }

            // Layer ① the English concept, ② the dictionary pile, ③ exam counts.
            // Printed in that order because looking a word up is meant to be
            // reading input rather than a switch into Chinese.
            let senses = entry.senses.prefix(4)
            for sense in senses {
                var line = "  \(sense.ordinal). "
                if let concept = sense.concept_en { line += concept + "  " }
                line += Self.gloss(sense.gloss_zh)
                if let exam = sense.exam {
                    line += Ink.dim("  〔真题 \(exam.frequency) 次"
                        + (exam.share.map { "，占 \($0)%" } ?? "") + "〕")
                }
                if entry.marks.additionalProperties[String(sense.id)] != nil {
                    line += Ink.red("  ← 你标记过这个义项")
                }
                out.append(line)
            }
            if entry.senses.count > senses.count {
                out.append(Ink.dim("  …还有 \(entry.senses.count - senses.count) 个义项"))
            }
            if entry.senses.isEmpty, let translation = entry.translation {
                out.append("  " + translation)
            }

            // Marks on *other* senses of the same word. Without this, a word you
            // know you marked reads as unmarked and the app looks like it forgot.
            let others = entry.marks.additionalProperties.filter {
                $0.key != String(token.headword.map { _ in 0 } ?? 0)
            }
            if !others.isEmpty, token.mark == nil {
                out.append(Ink.dim("  （你标记过这个词的另一个义项）"))
            }
            return out.joined(separator: "\n")
        }
    }

    /// `gloss_zh` is a list of strings or a single string, so the generator
    /// gives it both slots and fills one. Not worth normalising on the server:
    /// the shape reflects real data, where some entries carry several glosses
    /// and some carry one line.
    static func gloss(_ value: Components.Schemas.Sense.gloss_zhPayload?) -> String {
        guard let value else { return "" }
        if let list = value.value1 { return list.joined(separator: "；") }
        return value.value2 ?? ""
    }
}
