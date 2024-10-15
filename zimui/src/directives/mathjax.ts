/* 
Directive to handle DOM manipulation to add/remove MathJax everytime the page change

This is a bit hackhish but the only reliable solution found so far, and probably the 
most robust one.

The dynamic behavior of removing / adding back MathJax is wanted/necessary because we 
need to dynamically set the PageIndex macro to dynamically display proper figures 
/ equations / ... numbering.

This directive MUST be used only once in the whole Vue.JS application.

MathJax settings are an adaptation of libretexts.org settings, for MathJax 3 (including
extensions now removed or not yet supported or included by default).
*/
import type { DirectiveBinding } from 'vue'

let front: string | undefined = undefined

const frontFromTitle = function (title: string): string {
  // Computes front value from page title.
  // E.g. if page title is `1.2: The Scientific Method` then front is `1.2.`
  let front: string = ''
  if (title.includes(':')) {
    front = title.split(':')[0]
    if (front.includes('.')) {
      const parts: string[] = front.split('.')
      front = parts.map((int) => (int.includes('0') ? parseInt(int, 10) : int)).join('.')
    }
    front += '.'
  }
  return front
}

const removeMathJax = function () {
  const script = document.getElementById('mathjax-script')
  if (script) script.remove()
  if (window.MathJax) delete window.MathJax
}

const addMathJax = function (front: string) {
  window.MathJax = {
    section: front,
    tex: {
      tags: 'all',
      macros: {
        PageIndex: ['{' + front + '#1}'.toString(), 1]
      },
      autoload: {
        color: [],
        colorv2: ['color']
      },
      packages: { '[+]': ['noerrors', 'mhchem', 'tagFormat', 'color', 'cancel'] }
    },
    loader: {
      load: ['[tex]/noerrors', '[tex]/mhchem', '[tex]/tagFormat', '[tex]/colorv2', '[tex]/cancel']
    },
    svg: {
      scale: 0.85
    },
    options: {
      menuOptions: {
        settings: {
          zoom: 'Double-Click',
          zscale: '150%'
        }
      }
    }
  }
  const script = document.createElement('script', {
    id: 'mathjax-script'
  } as ElementCreationOptions)
  script.src = './mathjax/es5/tex-svg.js'
  script.async = false
  document.head.appendChild(script)
}

const vMathJax = {
  mounted(el: HTMLElement, binding: DirectiveBinding) {
    front = frontFromTitle(binding.value.title)
    removeMathJax() // "just-in-case"
    addMathJax(front)
  },
  updated(el: HTMLElement, binding: DirectiveBinding) {
    // Reload MathJax only if title has changed (allow to debounce update which might be
    // called multiple times)
    const new_front = frontFromTitle(binding.value.title)
    if (new_front == front) {
      return
    }
    front = new_front
    removeMathJax()
    addMathJax(front)
  },
  unmounted() {
    removeMathJax()
  }
}

export default vMathJax
