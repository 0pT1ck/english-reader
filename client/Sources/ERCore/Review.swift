import Foundation

/// 复习状态机 the review state machine
///
/// **Why this exists on the client at all.** 架构前提 1 says the client holds no
/// business logic, and this is the one place the offline requirement makes that
/// impossible: a day of review has to be finishable with no network, and the
/// day is a state machine — passing 看词想义 unlocks 看义想词, failing the second
/// locks it again. A client that could not work that out would stop at the
/// first question underground.
///
/// **So the rule is narrow: mirror, never invent.** Every transition below was
/// transcribed from the server's `session.answer`, and none of them is a
/// decision this file gets to make. `weightDecay` arrives in the payload rather
/// than being written here for the same reason — it is a tuning knob the server
/// owns, and what the client does is draw from a bag.
///
/// **And the mirror is checked.** `review-vectors.json` is exported from an
/// independent Python transcription of the same rules
/// (`scripts/review_reference.py`), and `ReviewStateTests` replays every case
/// through this type. When the two disagree it is this file that is wrong.
///
/// **2026-09-18 (P9): the thing being mirrored moved.** The server used to run
/// this state machine too and the vectors came from it; now it does not run it
/// at all. A mirror with nothing behind it is a mirror of itself, which is why
/// the reference was kept rather than deleted.
public enum ReviewDirection: Int, Sendable, Codable {
    /// 看词想义：给你这个词，想它的意思。
    case wordToSense = 1
    /// 看义想词：给你意思，想那个词。Unlocked by passing the first.
    case senseToWord = 2
}

/// Where one item stands in today's review.
///
/// A value type on purpose: a round is a sequence of small transitions, and
/// being able to write `state = state.applying(answer)` is what makes the
/// vectors replayable without a database or a fixture of mutable objects.
public struct ReviewItemState: Equatable, Sendable {
    public var direction: ReviewDirection
    /// How many times this item has been asked today, both directions together.
    /// **This is the grade** — behaviour rather than a self-rated difficulty.
    public var asks: Int
    public var misses: Int
    /// Draw weight. Halved on every failure so a stuck item stops blocking the
    /// rest, and floored rather than zeroed so it is never excluded outright.
    public var weight: Double
    /// Passed both directions today. A finished item is not asked again, and
    /// answering one is an error rather than a no-op.
    public var done: Bool
    /// "That was trivial." Claimed by the learner, honoured only on a round
    /// with no misses — a claim withdrawn by a miss, because it plainly was
    /// not. **Nobody checks it a second time as of P9**: the device is where
    /// the interval is computed now, so the rule has to hold here or nowhere.
    public var easy: Bool

    /// The floor a weight never goes below. Its purpose is the opposite of what
    /// a floor usually does: it keeps a hopeless item *in* the pool.
    public static let weightFloor = 0.0001

    public init(
        direction: ReviewDirection = .wordToSense,
        asks: Int = 0,
        misses: Int = 0,
        weight: Double = 1.0,
        done: Bool = false,
        easy: Bool = false
    ) {
        self.direction = direction
        self.asks = asks
        self.misses = misses
        self.weight = weight
        self.done = done
        self.easy = easy
    }
}

/// One answer, as the learner gave it.
public struct ReviewAnswer: Equatable, Sendable {
    public var passed: Bool
    /// Whether the hint was opened — 0 or 1 since P7 made 「不确定」 a single
    /// tap. Not acted on *here*: this type only moves the round along, while
    /// `Scheduler.grade(misses:easy:revealed:capped:)` is where it costs a
    /// grade. Both are on the device as of P9; before that the second half
    /// was the server's.
    public var revealed: Int
    public var easy: Bool

    public init(passed: Bool, revealed: Int = 0, easy: Bool = false) {
        self.passed = passed
        self.revealed = revealed
        self.easy = easy
    }
}

extension ReviewItemState {
    /// Apply one answer. Pure: the same state and answer always give the same
    /// result, which is what lets the exported vectors be a test rather than a
    /// description.
    ///
    /// - Parameter weightDecay: from the server's payload, never a constant here.
    /// - Parameter singleDirection: 这个条目只问一个方向。**词组就是这样**
    ///   （P11 决定 ⑤b）：看词组想意思问得了，反过来「给『导致』想英文」是道坏题——
    ///   `lead to`／`result in`／`contribute to` 全对，而卡片只认一个答案。
    public func applying(_ answer: ReviewAnswer, weightDecay: Double,
                         singleDirection: Bool = false) -> ReviewItemState {
        var next = self
        next.asks += 1
        if !answer.passed { next.misses += 1 }

        // Claimed, and only on a clean round. A miss withdraws an earlier
        // claim: it plainly was not easy.
        next.easy = (easy || answer.easy) && next.misses == 0

        switch (answer.passed, direction) {
        case (true, .wordToSense) where singleDirection:
            // 只有一个方向的条目，答对第一向就算走完这一轮——**没有第二向可解锁**。
            next.done = true
        case (true, .wordToSense):
            // Unlocked, then put back in the pool rather than asked straight
            // away — the gap is the point of asking the other way round.
            next.direction = .senseToWord
        case (true, .senseToWord):
            next.done = true
        case (false, _):
            // Failing either direction sends the item back to the first and
            // re-locks the second.
            next.direction = .wordToSense
            next.weight = max(Self.weightFloor, weight * weightDecay)
        }
        return next
    }

    /// Answering a finished item is a mistake, not a no-op. It used to be the
    /// server that raised on it; now the guard is only here, and a round that
    /// accepted one would put an event in the log that replays into a state
    /// nothing else can reach.
    public var acceptsAnswer: Bool { !done }
}

/// Drawing the next question.
///
/// Weighted, and the weight is the round's own — halved by each miss. The draw
/// is the one genuinely random thing in the day, which is why `random` is a
/// parameter: a replay has to be able to pin it.
public struct ReviewDraw: Sendable {
    public init() {}

    /// Pick one of the open items, weighted. `nil` when the day is done.
    ///
    /// - Parameter random: a value in 0..<1. Injected rather than taken from a
    ///   global generator so a test can pin the draw — and so the CLI can
    ///   replay a day.
    public func pick<T>(
        from items: [T],
        weight: (T) -> Double,
        isOpen: (T) -> Bool,
        random: Double
    ) -> T? {
        let open = items.filter(isOpen)
        guard !open.isEmpty else { return nil }

        let total = open.reduce(0.0) { $0 + max(weight($1), ReviewItemState.weightFloor) }
        var cursor = random * total
        for item in open {
            cursor -= max(weight(item), ReviewItemState.weightFloor)
            if cursor <= 0 { return item }
        }
        // Only reachable through floating-point drift at the very end of the
        // range; the last open item is the right answer there.
        return open.last
    }
}
