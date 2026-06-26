package com.nasaroai.agent

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class AccessibilityAgentService : AccessibilityService() {

    companion object {
        @Volatile private var instance: AccessibilityAgentService? = null

        /** FloatingService 등 UI 계층에서 민감 작업 확인 다이얼로그를 띄울 때 사용 */
        @Volatile var confirmHandler: ((String) -> Boolean)? = null

        fun isRunning(): Boolean = instance != null

        fun isEnabled(context: Context): Boolean {
            if (isRunning()) return true
            val enabled = Settings.Secure.getString(
                context.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
            ).orEmpty()
            return enabled.contains(context.packageName)
        }

        fun performTask(task: String): String {
            val service = instance
                ?: return "접근성 서비스가 아직 연결되지 않았습니다. 설정에서 Nasaro AI 에이전트를 켠 뒤, 앱을 한 번 재시작하거나 잠시 후 다시 시도해주세요."
            return service.handleTask(task)
        }
    }

    private val siteUrls = mapOf(
        "네이버 금융" to "https://m.finance.naver.com",
        "naver finance" to "https://m.finance.naver.com",
        "네이버증권" to "https://m.stock.naver.com",
        "네이버" to "https://m.naver.com",
        "구글" to "https://www.google.com",
        "youtube" to "https://m.youtube.com",
        "유튜브" to "https://m.youtube.com",
    )

    private val bankPackageHints = listOf(
        "bank", "toss", "kakaobank", "kakaopay", "kbstar", "shinhan", "kebhana",
        "wooribank", "nhbank", "hanabank", "citibank", "pay", "wallet",
    )

    private val sensitiveStepWords = listOf(
        "결제", "송금", "이체", "계좌", "카드", "은행", "페이", "비밀번호", "password", "pin",
    )

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit
    override fun onInterrupt() = Unit

    private fun handleTask(rawTask: String): String {
        val task = rawTask.trim()
        if (task.isBlank()) return "임무를 입력해주세요."

        val steps = splitSteps(task)
        return if (steps.size > 1) handleMultiStep(steps) else handleSingleStep(task)
    }

    /** "A해서 B하고 C" 형태를 단계별로 분리 */
    private fun splitSteps(task: String): List<String> {
        return task.split(Regex("(?:\\s*해서\\s*|\\s*하고\\s*|\\s*후에\\s*|\\s*다음\\s*|\\s*그리고\\s*|\\s*,\\s*)"))
            .map { it.trim().trimEnd('.', '!', '?') }
            .filter { it.isNotEmpty() }
    }

    private fun handleMultiStep(steps: List<String>): String {
        val lines = mutableListOf<String>()
        for ((index, step) in steps.withIndex()) {
            if (needsSensitiveConfirm(step, getCurrentForegroundPackage())) {
                val approved = requestUserConfirm(
                    "[${index + 1}/${steps.size}] '$step'\n\n결제·은행·계좌 관련 작업일 수 있습니다. 계속 진행할까요?",
                )
                if (!approved) {
                    lines.add("${index + 1}. $step → 사용자가 취소했습니다.")
                    break
                }
            }
            val result = handleSingleStep(step)
            lines.add("${index + 1}. $step → $result")
            if (result.contains("찾지 못했") && index < steps.lastIndex) {
                waitForUi(1200)
            } else {
                waitForUi(900)
            }
        }
        return lines.joinToString("\n")
    }

    private fun handleSingleStep(step: String): String {
        val lower = step.lowercase(Locale.ROOT)

        when {
            matchesAny(lower, listOf("홈", "home", "홈화면")) -> {
                performGlobalAction(GLOBAL_ACTION_HOME)
                return "홈 화면으로 이동했습니다."
            }
            matchesAny(lower, listOf("뒤로", "back", "이전")) -> {
                performGlobalAction(GLOBAL_ACTION_BACK)
                return "뒤로가기를 실행했습니다."
            }
            matchesAny(lower, listOf("최근", "멀티태스", "recent")) -> {
                performGlobalAction(GLOBAL_ACTION_RECENTS)
                return "최근 앱 화면을 열었습니다."
            }
        }

        // 사이트/URL 열기 (네이버 금융 들어가 등)
        resolveSiteUrl(step)?.let { url ->
            openUrl(url)
            return "브라우저에서 ${step} 페이지를 열었습니다."
        }

        // 확인·조회 (PER 확인해 등)
        if (lower.contains("확인") || lower.contains("조회") || lower.contains("알려") || lower.contains("per")) {
            val keywords = extractVerifyKeywords(step)
            if (keywords.isNotEmpty()) {
                val read = readScreenFor(keywords)
                if (read != null) return read
            }
        }

        if (lower.contains("스크롤") || lower.contains("내려") || lower.contains("아래로")) {
            return if (scrollForward()) "화면을 아래로 스크롤했습니다." else "스크롤할 영역을 찾지 못했습니다."
        }
        if (lower.contains("올려") || lower.contains("위로")) {
            return if (scrollBackward()) "화면을 위로 스크롤했습니다." else "스크롤할 영역을 찾지 못했습니다."
        }

        if (lower.contains("입력") || lower.contains("써") || lower.contains("적어")) {
            val text = extractAfterAny(step, listOf("입력", "써", "적어", "write", "type"))
            if (text.isBlank()) return "입력할 내용을 찾지 못했습니다."
            return if (typeIntoFocusedField(text)) "입력창에 '$text'를 입력했습니다."
            else "입력창을 찾지 못했습니다. 먼저 입력창을 눌러주세요."
        }

        // 앱/항목 열기·클릭
        val openTarget = extractOpenTarget(step)
        if (openTarget.isNotBlank()) {
            if (openAppByLabel(openTarget)) return "'$openTarget' 앱을 열었습니다."
            if (clickByText(openTarget)) return "'$openTarget' 항목을 눌렀습니다."
            // 검색창에 입력 후 엔터 시도
            if (typeIntoFocusedField(openTarget)) {
                waitForUi(400)
                performGlobalAction(GLOBAL_ACTION_BACK) // no - need enter. Try click search result after type
                if (clickByText(openTarget)) return "'$openTarget' 검색 후 선택했습니다."
            }
            return "화면에서 '$openTarget'을(를) 찾지 못했습니다."
        }

        return "단계를 이해하지 못했습니다: $step"
    }

    private fun resolveSiteUrl(step: String): String? {
        val lower = step.lowercase(Locale.ROOT)
        for ((name, url) in siteUrls) {
            if (lower.contains(name.lowercase(Locale.ROOT))) return url
        }
        if (lower.contains("http")) {
            val m = Regex("(https?://\\S+)").find(step)
            if (m != null) return m.groupValues[1]
        }
        return null
    }

    private fun extractOpenTarget(step: String): String {
        var s = step
        for (marker in listOf("열어", "들어가", "실행", "켜", "눌러", "클릭", "터치", "탭", "open", "launch")) {
            s = s.replace(marker, " ", ignoreCase = true)
        }
        return s
            .replace(Regex("(앱|app|해줘|해 주세요|확인|조회|please)", RegexOption.IGNORE_CASE), " ")
            .trim(' ', ':', '"', '\'', '을', '를', '에', '로', '.')
    }

    private fun extractVerifyKeywords(step: String): List<String> {
        val found = mutableListOf<String>()
        val lower = step.lowercase(Locale.ROOT)
        if (lower.contains("per")) found.add("PER")
        val tokens = step.split(Regex("\\s+"))
        for (t in tokens) {
            val clean = cleanTokenSuffix(t.trim('?', '.', '!'))
            if (clean.length >= 2 && !listOf("확인", "조회", "알려", "해줘").contains(clean)) {
                found.add(clean)
            }
        }
        return found.distinct()
    }

    private fun readScreenFor(keywords: List<String>): String? {
        val root = getTargetRoot() ?: return null
        val texts = mutableListOf<String>()
        collectTexts(root, texts)
        val hits = texts.filter { line ->
            keywords.any { kw -> line.contains(kw, ignoreCase = true) }
        }.distinct().take(8)
        if (hits.isEmpty()) {
            scrollForward()
            waitForUi(500)
            val root2 = getTargetRoot() ?: return null
            collectTexts(root2, texts)
            val hits2 = texts.filter { line ->
                keywords.any { kw -> line.contains(kw, ignoreCase = true) }
            }.distinct().take(8)
            if (hits2.isEmpty()) return null
            return "화면에서 확인:\n" + hits2.joinToString("\n")
        }
        return "화면에서 확인:\n" + hits.joinToString("\n")
    }

    private fun collectTexts(node: AccessibilityNodeInfo, out: MutableList<String>) {
        val t = node.text?.toString()?.trim().orEmpty()
        if (t.length in 2..120) out.add(t)
        val d = node.contentDescription?.toString()?.trim().orEmpty()
        if (d.length in 2..120) out.add(d)
        for (i in 0 until node.childCount) {
            node.getChild(i)?.let { collectTexts(it, out) }
        }
    }

    private fun needsSensitiveConfirm(step: String, packageName: String?): Boolean {
        val lower = step.lowercase(Locale.ROOT)
        if (sensitiveStepWords.any { lower.contains(it) }) return true
        if (packageName != null && bankPackageHints.any { packageName.contains(it, ignoreCase = true) }) {
            return true
        }
        return false
    }

    private fun getCurrentForegroundPackage(): String? {
        val root = getTargetRoot() ?: return null
        return root.packageName?.toString()
    }

    private fun requestUserConfirm(message: String): Boolean {
        confirmHandler?.let { return it(message) }
        return false
    }

    private fun waitForUi(ms: Long) {
        try { Thread.sleep(ms) } catch (_: InterruptedException) {}
    }

    private fun openUrl(url: String): Boolean {
        return try {
            val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            startActivity(intent)
            true
        } catch (_: Exception) {
            false
        }
    }

    private fun matchesAny(text: String, words: List<String>): Boolean =
        words.any { text.contains(it) }

    private fun cleanTokenSuffix(text: String): String {
        var s = text.trim()
        for (suffix in listOf("주세요", "해줘", "확인", "조회", "을", "를", "에", "로")) {
            if (s.endsWith(suffix)) s = s.removeSuffix(suffix).trim()
        }
        return s
    }

    private fun extractAfterAny(text: String, markers: List<String>): String {
        for (marker in markers) {
            val idx = text.indexOf(marker, ignoreCase = true)
            if (idx >= 0) {
                return cleanTokenSuffix(
                    text.substring(idx + marker.length)
                        .trim(' ', ':', '"', '\'', '을', '를', '에', '로', '해', '줘'),
                )
            }
        }
        return ""
    }

    private fun getTargetRoot(): AccessibilityNodeInfo? {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            for (window in windows.orEmpty()) {
                if (window.type != AccessibilityWindowInfo.TYPE_APPLICATION) continue
                val root = window.root ?: continue
                if (root.packageName?.toString() == packageName) continue
                return root
            }
        }
        val active = rootInActiveWindow ?: return null
        return if (active.packageName?.toString() == packageName) {
            findForeignRootFromActive(active)
        } else active
    }

    private fun findForeignRootFromActive(active: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            for (window in windows.orEmpty()) {
                val root = window.root ?: continue
                if (root.packageName?.toString() != packageName) return root
            }
        }
        return if (active.packageName?.toString() != packageName) active else null
    }

    private fun openAppByLabel(appName: String): Boolean {
        val pm = packageManager
        val apps = pm.getInstalledApplications(0)
        val target = apps.firstOrNull { app ->
            pm.getApplicationLabel(app).toString().contains(appName, ignoreCase = true)
        } ?: return false
        val launchIntent = pm.getLaunchIntentForPackage(target.packageName) ?: return false
        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        startActivity(launchIntent)
        return true
    }

    private fun clickByText(label: String): Boolean {
        if (label.isBlank()) return false
        val root = getTargetRoot() ?: return false
        val node = findNode(root) { info ->
            val text = info.text?.toString().orEmpty()
            val desc = info.contentDescription?.toString().orEmpty()
            text.contains(label, ignoreCase = true) || desc.contains(label, ignoreCase = true)
        } ?: return false

        var clickable: AccessibilityNodeInfo? = node
        while (clickable != null && !clickable.isClickable) clickable = clickable.parent
        if (clickable?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true) return true

        val rect = Rect()
        node.getBoundsInScreen(rect)
        if (rect.isEmpty) return false
        return tap(rect.centerX().toFloat(), rect.centerY().toFloat())
    }

    private fun typeIntoFocusedField(text: String): Boolean {
        val root = getTargetRoot() ?: return false
        val focused = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?: findNode(root) { it.isEditable }
            ?: return false
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        focused.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        return focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    private fun scrollForward(): Boolean {
        val root = getTargetRoot() ?: return false
        val node = findNode(root) { it.isScrollable } ?: root
        return node.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
    }

    private fun scrollBackward(): Boolean {
        val root = getTargetRoot() ?: return false
        val node = findNode(root) { it.isScrollable } ?: root
        return node.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD)
    }

    private fun findNode(
        node: AccessibilityNodeInfo,
        predicate: (AccessibilityNodeInfo) -> Boolean,
    ): AccessibilityNodeInfo? {
        if (predicate(node)) return node
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            findNode(child, predicate)?.let { return it }
        }
        return null
    }

    private fun tap(x: Float, y: Float): Boolean {
        val path = Path().apply { moveTo(x, y) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 80))
            .build()
        val latch = CountDownLatch(1)
        var ok = false
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) {
                ok = true
                latch.countDown()
            }
            override fun onCancelled(gestureDescription: GestureDescription?) {
                latch.countDown()
            }
        }, null)
        latch.await(1200, TimeUnit.MILLISECONDS)
        return ok
    }
}
