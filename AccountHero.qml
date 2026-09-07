import QtQuick
import qs.Commons

// Account heading with sentence-case supporting text.
Item {
  id: root
  property Component iconComponent: null
  property Component trailingControl: null
  property string title: ""
  property string meta: ""
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  implicitHeight: Math.max(icon.implicitHeight, labels.implicitHeight, trailing.implicitHeight)

  Loader {
    id: icon
    sourceComponent: root.iconComponent
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
  }

  Column {
    id: labels
    anchors.left: icon.right
    anchors.leftMargin: Style.space(14)
    anchors.right: trailing.left
    anchors.rightMargin: trailing.width > 0 ? Style.space(12) : 0
    anchors.verticalCenter: parent.verticalCenter
    spacing: Style.space(2)

    Text {
      width: parent.width
      text: root.title
      textFormat: Text.PlainText
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.title
      font.bold: true
      elide: Text.ElideRight
    }
    Text {
      width: parent.width
      text: root.meta
      textFormat: Text.PlainText
      visible: text !== ""
      color: Qt.darker(root.foreground, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }
  }

  Loader {
    id: trailing
    sourceComponent: root.trailingControl
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
  }
}
