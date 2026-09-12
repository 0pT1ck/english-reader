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
/// **So the rule is narrow: mirror, never invent.** Every transition below is
/// one the server performs in `session.answer`, and none of them is a decision
/// this file gets to make. `weightDecay` arrives in the payload rather than
/// being written here for the same reason — the rule is the server's, and what
/// the client does is draw from a bag.
///
/// **And the mirror is checked.** `review-vectors.json` is exported from the
/// server's own code, and `ReviewStateTests` replays every case through this
/// type. When the two disagree it is this file that is wrong.
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
    /// with no misses — and the server checks, so this is a mirror of its
    /// decision rather than a way to talk the interval longer.
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
    /// How far the hints had to be opened, 0–3. Recorded, not acted on here:
    /// the server turns it into a grade, the client only reports it.
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
    public func applying(_ answer: ReviewAnswer, weightDecay: Double) -> ReviewItemState {
        var next = self
        next.asks += 1
        if !answer.passed { next.misses += 1 }

        // Claimed, and only on a clean round. A miss withdraws an earlier
        // claim: it plainly was not easy.
        next.easy = (easy || answer.easy) && next.misses == 0

        switch (answer.passed, direction) {
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

    /// Answering a finished item is a mistake, not a no-op — the server raises
    /// on it, so a client that let it happen would collect failures in its
    /// outbox that no retry can ever clear.
    public var acceptsAnswer: Bool { !done }
}

/// Drawing the next question.
///
/// Weighted, and the weights come from the server. The client picks from a bag;
/// it does not decide what is in it.
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
