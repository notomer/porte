import AppKit
import UniformTypeIdentifiers

/// The two document types Porte accepts from Finder.
///
/// `.exe` already has a system-declared identifier; Porte only offers itself as
/// an alternate handler. `.msi` has no system declaration on macOS, so Porte
/// imports a type declaration of its own. That is the same relationship the
/// system has with the format: Microsoft defines it, Porte recognises it.
public enum PorteFileType: String, CaseIterable, Sendable {
    case windowsExecutable
    case windowsInstaller

    public var identifier: String {
        switch self {
        case .windowsExecutable: return "com.microsoft.windows-executable"
        case .windowsInstaller: return "com.notomer.porte.windows-msi"
        }
    }

    public var pathExtension: String {
        switch self {
        case .windowsExecutable: return "exe"
        case .windowsInstaller: return "msi"
        }
    }

    public var displayName: String {
        switch self {
        case .windowsExecutable: return "Windows applications (.exe)"
        case .windowsInstaller: return "Windows Installer packages (.msi)"
        }
    }

    public var contentType: UTType {
        switch self {
        case .windowsExecutable:
            return UTType(filenameExtension: "exe")
                ?? UTType(importedAs: identifier, conformingTo: .data)
        case .windowsInstaller:
            return UTType(identifier)
                ?? UTType(filenameExtension: "msi")
                ?? UTType(importedAs: identifier, conformingTo: .data)
        }
    }
}

public struct AssociationStatus: Equatable, Sendable {
    public let type: PorteFileType
    public let handlerPath: String?
    public let handlerName: String?
    public let portePath: String

    public var isPorte: Bool {
        guard let handlerPath else { return false }
        return URL(fileURLWithPath: handlerPath).resolvingSymlinksInPath().path
            == URL(fileURLWithPath: portePath).resolvingSymlinksInPath().path
    }

    public var summary: String {
        if isPorte { return "Porte" }
        return handlerName ?? "No application"
    }
}

public enum AssociationError: LocalizedError, Sendable {
    case notApplied(PorteFileType)

    public var errorDescription: String? {
        switch self {
        case .notApplied(let type):
            return "macOS did not switch .\(type.pathExtension) files to Porte."
        }
    }
}

public enum FileAssociations {
    public static func status(for type: PorteFileType,
                              porteApp: URL = Bundle.main.bundleURL) -> AssociationStatus {
        let handler = NSWorkspace.shared.urlForApplication(toOpen: type.contentType)
        let name = handler.flatMap { Bundle(url: $0)?.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String }
            ?? handler?.deletingPathExtension().lastPathComponent
        return AssociationStatus(type: type,
                                 handlerPath: handler?.path,
                                 handlerName: name,
                                 portePath: porteApp.path)
    }

    public static func allStatuses(porteApp: URL = Bundle.main.bundleURL) -> [AssociationStatus] {
        PorteFileType.allCases.map { status(for: $0, porteApp: porteApp) }
    }

    /// Uses the documented Launch Services API (`NSWorkspace.setDefaultApplication`).
    /// There is no database editing and no bypass of the system's decision.
    ///
    /// The Swift surface of that API reports no synchronous error, so the result
    /// is read back through Launch Services instead of being assumed.
    public static func setDefaultApplication(_ app: URL, for type: PorteFileType) throws {
        NSWorkspace.shared.setDefaultApplication(at: app, toOpen: type.contentType)
        for _ in 0..<12 {
            if status(for: type, porteApp: app).isPorte { return }
            Thread.sleep(forTimeInterval: 0.1)
        }
        throw AssociationError.notApplied(type)
    }

    public static func makePorteDefault(_ types: [PorteFileType] = PorteFileType.allCases,
                                        app: URL = Bundle.main.bundleURL) -> [PorteFileType: Error] {
        var failures: [PorteFileType: Error] = [:]
        for type in types {
            do {
                try setDefaultApplication(app, for: type)
            } catch {
                failures[type] = error
            }
        }
        return failures
    }

    /// Files the app will accept by drop or from the Open panel.
    public static func isWindowsProgram(_ url: URL) -> Bool {
        ["exe", "msi", "msix", "appx"].contains(url.pathExtension.lowercased())
    }

    public static func isLaunchableBundle(_ url: URL) -> Bool {
        url.pathExtension.lowercased() == "app"
    }

    public static let manualInstructions = """
    macOS only lets an application change its own default document handler. If \
    Porte cannot set itself as the default, select an .exe or .msi file in \
    Finder, choose File ▸ Get Info, pick Porte under “Open with”, then click \
    “Change All…”. This is the same approval macOS asks of every application.
    """
}
