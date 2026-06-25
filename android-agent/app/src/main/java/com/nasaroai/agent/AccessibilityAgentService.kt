package com.nasaroai.agent

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.Locale

class AccessibilityAgentService : AccessibilityService() {

    companion object {
        @Volatile private var instance: AccessibilityAgentService? = null

        fun isRunning(): Boolean = instance != null

        fun performTask(task: String): String {
            val service = instance ?: return "접근성 권한이 꺼져 있습니다. Android 설정에서 Nasaro AI 에이전트 접근성을 켠 뒤 다시 실행해주세요."
            return service.handleTask(task)
        }
    }

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
        val lower = task.lowercase(Locale.ROOT)

        return when {
            task.isBlank() -> "임무를 입력해주세요."
            lower.contains("홈") || lower.contains("home") -> {
                performGlobalAction(GLOBAL_ACTION_HOME)
                "홈 화면으로 이동했습니다."
            }
            lower.contains("뒤로") || lower.contains("back") -> {
                performGlobalAction(GLOBAL_ACTION_BACK)
                "뒤로가기를 실행했습니다."
            }
            lower.contains("최근") || lower.contains("멀티태스") || lower.contains("recent") -> {
                performGlobalAction(GLOBAL_ACTION_RECENTS)
                "최근 앱 화면을 열었습니다."
            }
            lower.contains("스크롤") || lower.contains("내려") -> {
                scrollForward()
                "화면을 아래로 스크롤했습니다."
            }
            lower.contains("올려") -> {
                scrollBackward()
                "화면을 위로 스크롤했습니다."
            }
            lower.contains("입력") || lower.contains("써") -> {
                val text = extractAfterAny(task, listOf("입력", "써", "적어", "write", "type"))
                if (text.isBlank()) "입력할 내용을 찾지 못했습니다. 예: '검색창에 나사로 AI 입력'"
                else if (typeIntoFocusedField(text)) "현재 입력창에 '$text'를 입력했습니다."
                else "현재 포커스된 입력창을 찾지 못했습니다. 먼저 입력창을 누른 뒤 다시 요청해주세요."
            }
            lower.contains("클릭") || lower.contains("눌러") || lower.contains("터치") -> {
                val label = extractClickableLabel(task)
                if (label.isBlank()) "누를 대상 텍스트를 찾지 못했습니다. 예: '로그인 버튼 눌러'"
                else if (clickByText(label)) "'$label' 항목을 눌렀습니다."
                else "현재 화면에서 '$label' 항목을 찾지 못했습니다."
            }
            lower.contains("열어") || lower.contains("들어가") || lower.contains("실행") -> {
                val appName = extractAfterAny(task, listOf("열어", "들어가", "실행", "open", "launch"))
                    .replace("앱", "")
                    .trim()
                if (appName.isBlank()) "열 앱 이름을 찾지 못했습니다. 예: '유튜브 열어'"
                else if (openAppByLabel(appName)) "'$appName' 앱을 열었습니다."
                else "설치된 앱 목록에서 '$appName'을 찾지 못했습니다."
            }
            else -> "Android 접근성 엔진이 이해한 기본 명령이 없습니다. 홈, 뒤로, 앱 열기, 텍스트 클릭, 입력, 스크롤 명령으로 요청해주세요."
        }
    }

    private fun extractAfterAny(text: String, markers: List<String>): String {
        for (marker in markers) {
            val idx = text.indexOf(marker, ignoreCase = true)
            if (idx >= 0) return text.substring(idx + marker.length).trim(' ', ':', '"', '\'', '을', '를')
        }
        return ""
    }

    private fun extractClickableLabel(text: String): String {
        return text
            .replace("클릭", "")
            .replace("눌러", "")
            .replace("터치", "")
            .replace("버튼", "")
            .trim(' ', ':', '"', '\'', '을', '를')
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
        val root = rootInActiveWindow ?: return false
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
        val root = rootInActiveWindow ?: return false
        val focused = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?: findNode(root) { it.isFocused && it.isEditable }
            ?: return false
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    private fun scrollForward(): Boolean {
        val root = rootInActiveWindow ?: return false
        val node = findNode(root) { it.isScrollable } ?: root
        return node.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
    }

    private fun scrollBackward(): Boolean {
        val root = rootInActiveWindow ?: return false
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
            val found = findNode(child, predicate)
            if (found != null) return found
        }
        return null
    }

    private fun tap(x: Float, y: Float): Boolean {
        val path = Path().apply { moveTo(x, y) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 80))
            .build()
        return dispatchGesture(gesture, null, null)
    }
}
