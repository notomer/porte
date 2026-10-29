import AppKit
import PorteKit
import SwiftUI

@main
struct PorteMain: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel.shared

    var body: some Scene {
        Window("Porte", id: "main") {
            RootView()
                .environmentObject(model)
                .frame(minWidth: 660, minHeight: 460)
        }
        .defaultSize(width: 880, height: 600)
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Open Windows App…") {
                    model.chooseFile()
                }
                .keyboardShortcut("o", modifiers: .command)
                .disabled(model.phase == .launching)
            }
            CommandGroup(after: .newItem) {
                Button("Refresh Library") {
                    model.refreshLibrary()
                }
                .keyboardShortcut("r", modifiers: .command)
                .disabled(model.phase != .ready)
            }
        }

        Settings {
            SettingsView()
                .environmentObject(model)
                .frame(width: 620, height: 520)
        }
    }
}

/// Finder, Open With and Dock drops all arrive here as Launch Services document
/// requests. Files can be delivered before the window has finished restoring, so
/// the model queues them; nothing is dropped on the floor.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        _ = MainActor.assumeIsolated {
            Task { await AppModel.shared.prepare() }
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        MainActor.assumeIsolated {
            AppModel.shared.open(urls: urls)
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication,
                                       hasVisibleWindows flag: Bool) -> Bool {
        MainActor.assumeIsolated {
            AppModel.shared.showWindow()
        }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
