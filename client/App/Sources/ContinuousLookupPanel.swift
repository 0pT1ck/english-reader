import SwiftUI
import ERCore

/// Continuous panel / 无级面板: content pans resize until maximum; the handle always resizes.
struct ContinuousLookupPanel: View {
    let item: LookupItem
    let maximumHeight: CGFloat
    let onSelect: (LookupItem.Selection) -> Void
    let onMark: (MarkKind?) -> Void
    let onDismiss: () -> Void

    @State private var requestedHeight: CGFloat?
    @State private var defaultContentHeight: CGFloat = 0
    @State private var footerHeight: CGFloat = 0
    @State private var chromeHeight: CGFloat = 44
    @State private var resizeSession: ResizeSession?

    private enum ResizeSession {
        case resizing(CGFloat)
        case ignored
    }

    private var maximum: CGFloat { max(1, maximumHeight) }
    private var minimum: CGFloat { min(maximum, max(180, chromeHeight + footerHeight + 60)) }
    private var defaultHeight: CGFloat {
        let measured = defaultContentHeight > 0
            ? defaultContentHeight + chromeHeight + footerHeight + 30 : 360
        return min(maximum, max(minimum, min(measured, maximum / 2)))
    }
    private var height: CGFloat { min(maximum, max(0, requestedHeight ?? defaultHeight)) }
    private var isResizing: Bool {
        if case .resizing? = resizeSession { return true }
        return false
    }
    private var isMaximum: Bool { height >= maximum - 1 && !isResizing }

    var body: some View {
        VStack(spacing: 0) {
            controls
            LookupSheet(item: item, isMaximum: isMaximum,
                        resizeGesture: resizeGesture(handle: false),
                        onSelect: onSelect, onMark: onMark,
                        onDefaultHeight: { defaultContentHeight = $0 },
                        onFooterHeight: { footerHeight = $0 })
        }
        .frame(height: height)
        .frame(maxWidth: .infinity)
        .background(.regularMaterial, in: UnevenRoundedRectangle(
            topLeadingRadius: 20, topTrailingRadius: 20))
        .clipShape(UnevenRoundedRectangle(topLeadingRadius: 20, topTrailingRadius: 20))
        .shadow(color: .black.opacity(0.15), radius: 12, y: -3)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("lookup-panel")
        .accessibilityAction(.escape, onDismiss)
        .onChange(of: maximumHeight) { old, _ in
            if let requestedHeight {
                self.requestedHeight = requestedHeight >= old - 1 ? maximum : min(requestedHeight, maximum)
            }
        }
        .onAppear {
            #if DEBUG
            if DevLaunch.expandLookup { requestedHeight = maximum }
            #endif
        }
    }

    private var controls: some View {
        HStack(spacing: 4) {
            Capsule()
                .fill(.secondary.opacity(0.5))
                .frame(width: 36, height: 5)
                .frame(maxWidth: .infinity, minHeight: 44)
                .contentShape(Rectangle())
                .gesture(resizeGesture(handle: true))
                .accessibilityElement()
                .accessibilityLabel("查词面板高度")
                .accessibilityIdentifier("lookup-handle")
                .accessibilityValue(isMaximum ? "最大" : "可调整")
                .accessibilityAdjustableAction { direction in
                    switch direction {
                    case .increment: changeHeight(min(maximum, height + 100))
                    case .decrement: changeHeight(max(minimum, height - 100))
                    @unknown default: break
                    }
                }
                .accessibilityAction(named: Text("展开到最大")) { changeHeight(maximum) }
                .accessibilityAction(named: Text("收起到默认高度")) { changeHeight(defaultHeight) }
                .accessibilityAction(named: Text("关闭查词面板"), onDismiss)
            Button {
                changeHeight(isMaximum ? defaultHeight : maximum)
            } label: {
                Image(systemName: isMaximum ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
                    .frame(width: 44, height: 44)
            }
            .accessibilityLabel(isMaximum ? "收起查词面板" : "展开查词面板")
            Button(action: onDismiss) {
                Image(systemName: "xmark").frame(width: 44, height: 44)
            }
            .accessibilityLabel("关闭查词面板")
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 12)
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { chromeHeight = $0 }
    }

    private func resizeGesture(handle: Bool) -> AnyGesture<DragGesture.Value> {
        AnyGesture(DragGesture(minimumDistance: 12, coordinateSpace: .global)
            .onChanged { value in
                if resizeSession == nil {
                    let vertical = abs(value.translation.height) > abs(value.translation.width)
                    resizeSession = vertical && (handle || !isMaximum) ? .resizing(height) : .ignored
                }
                guard case .resizing(let start)? = resizeSession else { return }
                requestedHeight = min(maximum, max(0, start - value.translation.height))
            }
            .onEnded { value in
                guard case .resizing? = resizeSession else {
                    resizeSession = nil
                    return
                }
                resizeSession = nil
                if value.velocity.height < -850 {
                    changeHeight(maximum)
                } else if height < minimum * 0.65 {
                    onDismiss()
                } else if height < minimum {
                    changeHeight(minimum)
                }
                // Ordinary drags deliberately retain their exact height, without detent snapping.
            })
    }


    private func changeHeight(_ value: CGFloat) {
        withAnimation(.easeOut(duration: 0.22)) { requestedHeight = value }
    }
}
