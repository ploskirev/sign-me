// A native backdrop behind Qt's content view. No private macOS APIs.
#import <Cocoa/Cocoa.h>
#import <objc/runtime.h>

static char backdropKey;

@interface TalonBackdrop : NSVisualEffectView
@end
@implementation TalonBackdrop
- (NSView *)hitTest:(NSPoint)point { return nil; }
@end

int talon_install_backdrop(void *handle) {
    if (![NSThread isMainThread] || !handle) return 0;
    NSView *view = (__bridge NSView *)handle;
    NSWindow *window = view.window;
    NSView *host = view.superview;
    if (!window || !host) return 0;
    TalonBackdrop *effect = objc_getAssociatedObject(view, &backdropKey);
    if (!effect) {
        effect = [[TalonBackdrop alloc] initWithFrame:view.frame];
        effect.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
        effect.material = NSVisualEffectMaterialUnderWindowBackground;
        effect.blendingMode = NSVisualEffectBlendingModeBehindWindow;
        effect.state = NSVisualEffectStateActive;
        objc_setAssociatedObject(view, &backdropKey, effect, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    }
    if (effect.superview != host) {
        [host addSubview:effect positioned:NSWindowBelow relativeTo:view];
    }
    effect.frame = view.frame;
    window.opaque = NO;
    window.backgroundColor = NSColor.clearColor;
    return 1;
}

void talon_set_backdrop_dark(void *handle, int dark) {
    NSView *view = (__bridge NSView *)handle;
    TalonBackdrop *effect = objc_getAssociatedObject(view, &backdropKey);
    effect.appearance = [NSAppearance appearanceNamed:dark ? NSAppearanceNameDarkAqua : NSAppearanceNameAqua];
}

long talon_window_number(void *handle) {
    return ((__bridge NSView *)handle).window.windowNumber;
}

int talon_backdrop_ready(void *handle) {
    NSView *view = (__bridge NSView *)handle;
    TalonBackdrop *effect = objc_getAssociatedObject(view, &backdropKey);
    return effect && effect.superview == view.superview && !view.window.opaque && !view.opaque
        && NSEqualRects(effect.frame, view.frame)
        && effect.blendingMode == NSVisualEffectBlendingModeBehindWindow;
}
