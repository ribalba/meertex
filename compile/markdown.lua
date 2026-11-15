-- The Lua filter of compile/service.py for a Markdown main file (docs/DESIGN.md §14, Markdown).
--
-- pandoc reads the file as CommonMark with `sourcepos`, which gives every
-- element its place in the file: a `data-pos` attribute, or a Span or Div
-- around it with `wrapper="1"`. This filter turns those places into LaTeX
-- comments that TeX never sees, and the service reads them back from the
-- .tex to map SyncTeX's lines to lines of the Markdown file:
--
-- * Every paragraph and list item starts with `%meertex:N` on a line of its
--   own, where N is the Markdown line it starts on.
-- * Every soft line break gets ` %meertex:N` at the end of its line, where N
--   is the Markdown line the next line comes from. The space before the `%`
--   stands for the line break, which the comment eats. A hard line break
--   (`\\`) is followed by the mark on a line of its own.
-- * Any other block (a heading, code, a table, a figure, a list) has
--   `%meertex:N` as a block of its own before it.
--
-- pandoc writes with --wrap=preserve, so the lines of a paragraph stay the
-- lines of the Markdown. Then the wrappers go and the attributes lose their
-- places, so the LaTeX is the same as without `sourcepos`. Without
-- `sourcepos` (the service's latexdiff) the filter only sets the defaults.
--
-- Only the service passes this filter. A document cannot name one.

local MARK = "%meertex:"

local function is_wrapper(el)
  return el.attr ~= nil and el.attr.attributes["wrapper"] == "1"
end

-- The first line of a place: "notes.md@9:1-12:1", "45:7-46:1;46:5-47:1".
local function first_line(el)
  if el.attr == nil then
    return nil
  end
  local pos = el.attr.attributes["data-pos"]
  if pos == nil or pos == "" then
    return nil
  end
  local line = pos:match(".*@(%d+):") or pos:match("^(%d+):")
  return line and tonumber(line)
end

-- "SoftBreak" or "LineBreak" for a line break, also one in a wrapper.
local function line_break(el)
  if el.t == "Span" and is_wrapper(el) and #el.content == 1 then
    el = el.content[1]
  end
  if el.t == "SoftBreak" or el.t == "LineBreak" then
    return el.t
  end
end

-- A line break says where the next line starts.
local function mark_breaks(inlines)
  local out = pandoc.Inlines({})
  for i, el in ipairs(inlines) do
    local kind = line_break(el)
    local line
    if kind then
      for j = i + 1, #inlines do
        line = first_line(inlines[j])
        if line then
          break
        end
      end
    end
    if not line then
      out:insert(el)
    elseif kind == "SoftBreak" then
      out:insert(pandoc.Space())
      out:insert(pandoc.RawInline("latex", MARK .. line))
      out:insert(pandoc.SoftBreak())
    else
      out:insert(pandoc.LineBreak())
      out:insert(pandoc.RawInline("latex", MARK .. line))
      out:insert(pandoc.SoftBreak())
    end
  end
  return out
end

local TEXT_BLOCKS = { Para = true, Plain = true }

-- The block ``block`` starts on ``line``. Its text starts with the mark, or
-- the mark comes before it.
local function mark_block(out, block, line)
  if TEXT_BLOCKS[block.t] then
    local content = pandoc.Inlines({ pandoc.RawInline("latex", MARK .. line), pandoc.SoftBreak() })
    content:extend(block.content)
    block.content = content
    out:insert(block)
  else
    out:insert(pandoc.RawBlock("latex", MARK .. line))
    out:insert(block)
  end
end

local function mark_blocks(blocks)
  local out = pandoc.Blocks({})
  for _, block in ipairs(blocks) do
    local line = first_line(block)
    if block.t == "Div" and is_wrapper(block) then
      for k, inner in ipairs(block.content) do
        if k == 1 and line then
          mark_block(out, inner, line)
        else
          out:insert(inner)
        end
      end
    elseif line then
      mark_block(out, block, line)
    else
      out:insert(block)
    end
  end
  return out
end

local function drop_place(el)
  if el.attr ~= nil and el.attr.attributes["data-pos"] ~= nil then
    local attr = el.attr
    attr.attributes["data-pos"] = nil
    el.attr = attr
    return el
  end
end

local function unwrap(el)
  if is_wrapper(el) then
    return el.content
  end
  return drop_place(el)
end

-- The boxes of a task list (`- [ ]`, `- [x]`), which pdfLaTeX cannot set as
-- characters and Latin Modern does not have.
local BOXES = { ["☐"] = "$\\square$", ["☒"] = "$\\boxtimes$" }

local function box(str)
  local tex = BOXES[str.text]
  if tex then
    return pandoc.RawInline("latex", tex)
  end
end

-- A page that reads well without a front matter. The front matter can set
-- every one of these.
local DEFAULTS = {
  geometry = "margin=2.5cm",
  colorlinks = true,
}

return {
  {
    Pandoc = function(doc)
      doc.blocks = doc.blocks:walk({ Inlines = mark_breaks })
      doc.blocks = doc.blocks:walk({ Blocks = mark_blocks })
      return doc
    end,
  },
  {
    Span = unwrap,
    Div = unwrap,
    Header = drop_place,
    CodeBlock = drop_place,
    Code = drop_place,
    Link = drop_place,
    Image = drop_place,
    Table = drop_place,
    Figure = drop_place,
    Str = box,
  },
  {
    Meta = function(meta)
      for key, value in pairs(DEFAULTS) do
        if meta[key] == nil then
          meta[key] = value
        end
      end
      return meta
    end,
  },
}
