/* Porte app launcher.
 *
 * This binary is copied into every generated bundle as
 * Contents/MacOS/<name>. It owns the macOS application identity (Dock icon,
 * menu bar, window, activation, termination) and it supervises the shared
 * Porte runtime, which is a separate process that executes the Windows PE.
 *
 * Separation is deliberate:
 *   launcher  AppKit lifecycle + user consent + process supervision
 *   runtime   PE loading, Win32 stubs, translation, graphics, caching
 *
 * The launcher never loads the Windows image into its own address space and
 * never writes inside the bundle. All mutable state lives in
 * ~/Library/Application Support/Porte/Applications/<bundle id>/.
 */
#import <Cocoa/Cocoa.h>
#include <signal.h>
#include <time.h>

static NSString *const kMetadataRelative = @"Contents/Resources/PorteApp.json";

static uint64_t NowNs(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

/* Earliest point the launcher can observe, so the runtime can attribute
 * start-up to the AppKit phase separately from Python and PE loading. */
static uint64_t gProcessStartNs = 0;

@interface PorteAppDelegate : NSObject <NSApplicationDelegate>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) NSTextView *logView;
@property(nonatomic, strong) NSTask *task;
@property(nonatomic, strong) NSDictionary *metadata;
@property(nonatomic, copy) NSString *appName;
@property(nonatomic, copy) NSString *supportDir;
@property(nonatomic) BOOL terminating;
@end

@implementation PorteAppDelegate

- (void)appendLine:(NSString *)line {
    dispatch_async(dispatch_get_main_queue(), ^{
        NSTextStorage *storage = self.logView.textStorage;
        [storage appendAttributedString:[[NSAttributedString alloc]
            initWithString:[line stringByAppendingString:@"\n"]]];
        [self.logView scrollRangeToVisible:NSMakeRange(storage.length, 0)];
    });
}

- (void)failWithTitle:(NSString *)title message:(NSString *)message {
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = title;
    alert.informativeText = message;
    [alert addButtonWithTitle:@"Quit"];
    [alert runModal];
    [NSApp terminate:nil];
}

- (NSString *)supportPath {
    NSString *base = [NSSearchPathForDirectoriesInDomains(
        NSApplicationSupportDirectory, NSUserDomainMask, YES) firstObject];
    return [base stringByAppendingPathComponent:
        [@"Porte/Applications" stringByAppendingPathComponent:
            self.metadata[@"bundle_id"]]];
}

- (BOOL)ensureConsent {
    NSString *consent = [self.supportDir stringByAppendingPathComponent:@"consent.json"];
    if ([[NSFileManager defaultManager] fileExistsAtPath:consent]) {
        return YES;
    }
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = [NSString stringWithFormat:@"Run “%@”?", self.appName];
    alert.informativeText = [NSString stringWithFormat:
        @"This application runs a Windows program through Porte:\n\n%@\n\n"
         "Windows binaries are not native macOS software. Porte provides a "
         "compatibility layer; it does not make an untrusted program safe. "
         "Only continue if you trust the source of this executable.",
        self.metadata[@"pe_display_path"] ?: self.metadata[@"pe"]];
    [alert addButtonWithTitle:@"Run"];
    [alert addButtonWithTitle:@"Quit"];
    if ([alert runModal] != NSAlertFirstButtonReturn) {
        return NO;
    }
    [[NSFileManager defaultManager] createDirectoryAtPath:self.supportDir
                              withIntermediateDirectories:YES attributes:nil error:nil];
    NSDictionary *record = @{@"bundle_id": self.metadata[@"bundle_id"],
                             @"pe_sha256": self.metadata[@"pe_sha256"],
                             @"accepted_at": @([[NSDate date] timeIntervalSince1970]),
                             @"accepted_by": @"user"};
    NSData *data = [NSJSONSerialization dataWithJSONObject:record options:NSJSONWritingPrettyPrinted error:nil];
    [[NSFileManager defaultManager] createFileAtPath:consent contents:data attributes:nil];
    return YES;
}

- (void)buildMenu {
    NSMenu *main = [[NSMenu alloc] init];
    NSMenuItem *appItem = [[NSMenuItem alloc] init];
    [main addItem:appItem];
    NSMenu *appMenu = [[NSMenu alloc] init];
    [appMenu addItemWithTitle:[NSString stringWithFormat:@"About %@", self.appName]
                       action:@selector(orderFrontStandardAboutPanel:)
                keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:[NSString stringWithFormat:@"Hide %@", self.appName]
                       action:@selector(hide:) keyEquivalent:@"h"];
    [appMenu addItemWithTitle:@"Hide Others" action:@selector(hideOtherApplications:)
                keyEquivalent:@"h"].keyEquivalentModifierMask =
        NSEventModifierFlagCommand | NSEventModifierFlagOption;
    [appMenu addItemWithTitle:@"Show All" action:@selector(unhideAllApplications:)
                keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:[NSString stringWithFormat:@"Quit %@", self.appName]
                       action:@selector(terminate:) keyEquivalent:@"q"];
    appItem.submenu = appMenu;

    NSMenuItem *editItem = [[NSMenuItem alloc] init];
    [main addItem:editItem];
    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"Edit"];
    [editMenu addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
    [editMenu addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
    editItem.submenu = editMenu;
    [NSApp setMainMenu:main];
}

- (BOOL)loadMetadata {
    NSString *path = [[[NSBundle mainBundle] bundlePath]
        stringByAppendingPathComponent:kMetadataRelative];
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (!data) {
        [self failWithTitle:@"Bundle metadata missing"
                    message:[NSString stringWithFormat:
                        @"Expected %@. The application bundle is incomplete; "
                         "re-run `porte app install` for the original .exe.", path]];
        return NO;
    }
    NSError *error = nil;
    self.metadata = [NSJSONSerialization JSONObjectWithData:data options:0 error:&error];
    if (![self.metadata isKindOfClass:[NSDictionary class]]) {
        [self failWithTitle:@"Bundle metadata unreadable"
                    message:error.localizedDescription ?: @"Not a JSON object."];
        return NO;
    }
    self.appName = self.metadata[@"name"] ?: @"Porte Application";
    return YES;
}

- (NSString *)pePath {
    NSString *relative = self.metadata[@"pe"];
    if (!relative) return nil;
    return [[[NSBundle mainBundle] bundlePath]
        stringByAppendingPathComponent:[@"Contents/Resources" stringByAppendingPathComponent:relative]];
}

- (NSArray<NSString *> *)runtimeCommand:(NSString *)bundlePath {
    NSDictionary *runtime = self.metadata[@"runtime"];
    if (![runtime isKindOfClass:[NSDictionary class]]) return nil;
    NSString *python = runtime[@"python"];
    NSString *root = runtime[@"root"];
    NSString *module = runtime[@"module"] ?: @"porte.app.runtime";
    if (![python isKindOfClass:[NSString class]] ||
        ![root isKindOfClass:[NSString class]]) return nil;
    NSFileManager *fm = [NSFileManager defaultManager];
    if (![fm isExecutableFileAtPath:python] || ![fm fileExistsAtPath:root]) return nil;
    return @[python, @"-u", @"-m", module, bundlePath];
}

- (void)windowWillClose:(NSNotification *)note {
    [NSApp terminate:nil];
}

- (void)applicationDidFinishLaunching:(NSNotification *)note {
    [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
    if (![self loadMetadata]) return;
    [self buildMenu];

    NSRect frame = NSMakeRect(0, 0, 640, 360);
    self.window = [[NSWindow alloc]
        initWithContentRect:frame
                  styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                             NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
                    backing:NSBackingStoreBuffered
                      defer:NO];
    self.window.title = self.appName;
    self.window.minSize = NSMakeSize(420, 220);
    [self.window setFrameAutosaveName:@"PorteLogWindow"];

    NSScrollView *scroll = [[NSScrollView alloc] initWithFrame:frame];
    scroll.hasVerticalScroller = YES;
    scroll.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.logView = [[NSTextView alloc] initWithFrame:scroll.bounds];
    self.logView.editable = NO;
    self.logView.selectable = YES;
    self.logView.font = [NSFont monospacedSystemFontOfSize:11 weight:NSFontWeightRegular];
    self.logView.textContainerInset = NSMakeSize(10, 10);
    self.logView.autoresizingMask = NSViewWidthSizable;
    scroll.documentView = self.logView;
    self.window.contentView = scroll;
    self.window.delegate = (id)self;
    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];

    self.supportDir = [self supportPath];
    [[NSFileManager defaultManager] createDirectoryAtPath:self.supportDir
                              withIntermediateDirectories:YES attributes:nil error:nil];

    NSString *pe = [self pePath];
    if (!pe || ![[NSFileManager defaultManager] fileExistsAtPath:pe]) {
        [self failWithTitle:@"Windows executable missing"
                    message:[NSString stringWithFormat:@"Expected %@ inside the bundle.",
                        pe ?: @"a PE recorded in PorteApp.json"]];
        return;
    }
    if (![self ensureConsent]) {
        [NSApp terminate:nil];
        return;
    }

    NSString *bundlePath = [[NSBundle mainBundle] bundlePath];
    NSArray<NSString *> *command = [self runtimeCommand:bundlePath];
    if (!command) {
        [self failWithTitle:@"Porte runtime unavailable"
                    message:[NSString stringWithFormat:
                        @"This application needs the shared Porte runtime at:\n%@\n\n"
                         "Reinstall Porte or re-run `porte app install` for this executable. "
                         "The bundle itself contains the Windows program and its profile; "
                         "nothing was changed.",
                        self.metadata[@"runtime"][@"root"] ?: @"(no runtime recorded)"]];
        return;
    }

    [self appendLine:[NSString stringWithFormat:@"%@ %@", self.appName,
                      self.metadata[@"version"] ?: @""]];
    [self appendLine:[NSString stringWithFormat:@"Windows image: %@",
                      self.metadata[@"pe_display_path"] ?: self.metadata[@"pe"]]];
    [self appendLine:@"Starting Porte runtime…"];

    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:command[0]];
    task.arguments = [command subarrayWithRange:NSMakeRange(1, command.count - 1)];
    task.currentDirectoryURL = [NSURL fileURLWithPath:self.metadata[@"runtime"][@"root"]];
    NSMutableDictionary *env = [[[NSProcessInfo processInfo] environment] mutableCopy];
    NSArray *pythonpath = self.metadata[@"runtime"][@"pythonpath"];
    env[@"PYTHONPATH"] = [pythonpath isKindOfClass:[NSArray class]] && pythonpath.count
        ? [pythonpath componentsJoinedByString:@":"]
        : self.metadata[@"runtime"][@"root"];
    env[@"PYTHONUNBUFFERED"] = @"1";
    env[@"PORTE_APP_BUNDLE"] = bundlePath;
    env[@"PORTE_LAUNCH_TS_NS"] = [NSString stringWithFormat:@"%llu", gProcessStartNs];
    env[@"PORTE_SPAWN_TS_NS"] = [NSString stringWithFormat:@"%llu", NowNs()];
    task.environment = env;

    NSPipe *pipe = [NSPipe pipe];
    task.standardOutput = pipe;
    task.standardError = pipe;
    __weak PorteAppDelegate *weakSelf = self;
    pipe.fileHandleForReading.readabilityHandler = ^(NSFileHandle *handle) {
        NSData *chunk = [handle availableData];
        if (chunk.length == 0) return;
        NSString *text = [[NSString alloc] initWithData:chunk encoding:NSUTF8StringEncoding]
            ?: @"[non-UTF8 output]";
        for (NSString *line in [text componentsSeparatedByString:@"\n"]) {
            if (line.length) [weakSelf appendLine:line];
        }
    };
    task.terminationHandler = ^(NSTask *finished) {
        PorteAppDelegate *strong = weakSelf;
        if (!strong) return;
        [strong appendLine:[NSString stringWithFormat:
            @"\nPorte runtime exited with status %d. Quit with ⌘Q.",
            finished.terminationStatus]];
        if (finished.terminationStatus != 0) {
            NSAlert *alert = [[NSAlert alloc] init];
            alert.messageText = @"The Windows program did not run to completion";
            alert.informativeText = [NSString stringWithFormat:
                @"Porte exited with status %d. The log above and "
                 "%@/logs contain the details.",
                finished.terminationStatus, strong.supportDir];
            [alert addButtonWithTitle:@"OK"];
            [alert runModal];
        }
    };
    NSError *launchError = nil;
    if (![task launchAndReturnError:&launchError]) {
        [self failWithTitle:@"Could not start the Porte runtime"
                    message:launchError.localizedDescription ?: @"Unknown error"];
        return;
    }
    self.task = task;
}

- (void)applicationWillTerminate:(NSNotification *)note {
    self.terminating = YES;
    if (self.task && self.task.isRunning) {
        [self.task terminate];
        for (int i = 0; i < 40 && self.task.isRunning; i++) {
            usleep(50000);
        }
        if (self.task.isRunning) kill(self.task.processIdentifier, SIGKILL);
    }
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)app {
    return YES;
}

- (BOOL)applicationSupportsSecureRestorableState:(NSApplication *)app {
    return YES;
}

@end

int main(int argc, const char *argv[]) {
    gProcessStartNs = NowNs();
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        PorteAppDelegate *delegate = [[PorteAppDelegate alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
