import Foundation

/// Accepts Finder-delivered file requests in order and suppresses duplicates.
///
/// macOS can deliver the same open request more than once: a double-click on a
/// cold-launched app, an Open With while a window is restoring, or a Dock drop
/// that also produces an Apple Event. Suppressing the same path inside a short
/// window keeps one sheet from stacking on another without hiding a deliberate
/// re-open of the same file later.
public final class FileOpenQueue {
    private let dedupeWindow: TimeInterval
    private let now: @Sendable () -> Date
    private var recent: [String: Date] = [:]
    private let lock = NSLock()

    public init(dedupeWindow: TimeInterval = 2.0,
                now: @escaping @Sendable () -> Date = { Date() }) {
        self.dedupeWindow = dedupeWindow
        self.now = now
    }

    /// Returns the URLs worth acting on, in the order they arrived.
    @discardableResult
    public func accept(_ urls: [URL], at timestamp: Date? = nil) -> [URL] {
        let moment = timestamp ?? now()
        lock.lock()
        defer { lock.unlock() }
        for (key, date) in recent where moment.timeIntervalSince(date) > dedupeWindow {
            recent.removeValue(forKey: key)
        }
        var accepted: [URL] = []
        for url in urls {
            guard url.isFileURL else { continue }
            let key = url.standardizedFileURL.path
            if let seen = recent[key], moment.timeIntervalSince(seen) <= dedupeWindow {
                continue
            }
            recent[key] = moment
            accepted.append(url.standardizedFileURL)
        }
        return accepted
    }

    public func reset() {
        lock.lock()
        recent.removeAll()
        lock.unlock()
    }
}
