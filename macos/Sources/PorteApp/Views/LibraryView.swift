import AppKit
import PorteKit
import SwiftUI

struct LibraryView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Porte")
                .toolbar {
                    ToolbarItem(placement: .navigation) {
                        Button {
                            model.chooseFile()
                        } label: {
                            Label("Open", systemImage: "plus")
                        }
                        .help("Open a Windows program or installer")
                    }
                    ToolbarItem(placement: .primaryAction) {
                        Button {
                            model.refreshLibrary()
                        } label: {
                            Label("Refresh", systemImage: "arrow.clockwise")
                        }
                        .help("Refresh the installed applications")
                        .disabled(model.isRefreshing)
                    }
                }
                .modifier(LibrarySearch(active: model.library.applications.count >= 6,
                                        text: $model.searchText))
                .dropDestination(for: URL.self) { urls, _ in
                    model.open(urls: urls)
                    return true
                }
                .sheet(item: $model.detailApp) { app in
                    AppDetailView(app: app, product: model.product(for: app))
                        .environmentObject(model)
                }
        }
    }

    @ViewBuilder
    private var content: some View {
        ZStack(alignment: .topTrailing) {
            if model.library.applications.isEmpty {
                emptyState
            } else if model.filteredApplications.isEmpty {
                ContentUnavailableView.search(text: model.searchText)
            } else {
                grid
            }
            if model.isRefreshing {
                ProgressView()
                    .controlSize(.small)
                    .padding(10)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("Open a Windows app", systemImage: "square.and.arrow.down")
        } description: {
            Text("Drag an EXE or MSI here, or choose a file to get started.")
        } actions: {
            Button("Choose File…") {
                model.chooseFile()
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .contentShape(Rectangle())
        .onTapGesture { model.showWindow() }
    }

    private var grid: some View {
        ScrollView {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 152, maximum: 196),
                                         spacing: 18)],
                      alignment: .center, spacing: 18) {
                ForEach(model.filteredApplications) { app in
                    AppTile(app: app)
                        .environmentObject(model)
                }
            }
            .padding(24)
        }
    }
}

private struct LibrarySearch: ViewModifier {
    let active: Bool
    @Binding var text: String

    func body(content: Content) -> some View {
        if active {
            content.searchable(text: $text, placement: .toolbar,
                               prompt: "Search applications")
        } else {
            content
        }
    }
}

private struct AppTile: View {
    let app: InstalledApplication
    @EnvironmentObject private var model: AppModel
    @StateObject private var hover = ViewFlag()

    var body: some View {
        Button {
            model.launch(app)
        } label: {
            VStack(spacing: 8) {
                ZStack(alignment: .topTrailing) {
                    AppIconView(path: app.path, size: 64)
                    if hover.value {
                        Image(systemName: "play.circle.fill")
                            .font(.system(size: 18))
                            .symbolRenderingMode(.palette)
                            .foregroundStyle(.white, Color.accentColor)
                            .offset(x: 7, y: -7)
                            .accessibilityHidden(true)
                    }
                }
                Text(app.name)
                    .font(.callout.weight(.medium))
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                if !app.isReady {
                    StatusBadge(text: app.statusLabel, tint: .orange)
                }
            }
            .frame(width: 148, height: 124, alignment: .top)
            .padding(10)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(hover.value ? Color.primary.opacity(0.06) : Color.clear)
            )
            .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
        .onHover { hover.value = $0 }
        .contextMenu {
            Button("Open") { model.launch(app) }
            Button("Show in Finder") { model.reveal(app) }
            Button("Details…") { model.showDetails(app) }
            if let product = model.product(for: app) {
                Divider()
                Button("Repair “\(product.name)”") { model.repair(product) }
                Button("Uninstall “\(product.name)”…", role: .destructive) {
                    model.uninstall(app)
                }
            } else {
                Divider()
                Button("Remove…", role: .destructive) { model.uninstall(app) }
            }
        }
        .accessibilityLabel(app.name)
        .accessibilityHint(app.isReady
                           ? "Launches the Windows application"
                           : (app.problem ?? "This application cannot start"))
    }
}
