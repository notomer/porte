import PorteKit
import SwiftUI

struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Group {
            switch model.phase {
            case .launching:
                LaunchView()
            case .setup:
                SetupView()
            case .ready:
                LibraryView()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task {
            await model.prepare()
            await model.loadDiagnostics()
        }
        .sheet(item: $model.workflow) { _ in
            WorkflowSheet()
                .environmentObject(model)
        }
        .porteAlert($model.alert)
    }
}
