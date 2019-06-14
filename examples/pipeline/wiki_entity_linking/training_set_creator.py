# coding: utf-8
from __future__ import unicode_literals

import os
import re
import bz2
import datetime
from os import listdir

from examples.pipeline.wiki_entity_linking import run_el
from spacy.gold import GoldParse
from spacy.matcher import PhraseMatcher
from . import wikipedia_processor as wp, kb_creator

"""
Process Wikipedia interlinks to generate a training dataset for the EL algorithm
"""

ENTITY_FILE = "gold_entities.csv"


def create_training(entity_def_input, training_output):
    wp_to_id = kb_creator._get_entity_to_id(entity_def_input)
    _process_wikipedia_texts(wp_to_id, training_output, limit=100000000)  # TODO: full dataset   100000000


def _process_wikipedia_texts(wp_to_id, training_output, limit=None):
    """
    Read the XML wikipedia data to parse out training data:
    raw text data + positive instances
    """

    title_regex = re.compile(r'(?<=<title>).*(?=</title>)')
    id_regex = re.compile(r'(?<=<id>)\d*(?=</id>)')

    read_ids = set()

    entityfile_loc = training_output + "/" + ENTITY_FILE
    with open(entityfile_loc, mode="w", encoding='utf8') as entityfile:
        # write entity training header file
        _write_training_entity(outputfile=entityfile,
                               article_id="article_id",
                               alias="alias",
                               entity="WD_id",
                               start="start",
                               end="end")

        with bz2.open(wp.ENWIKI_DUMP, mode='rb') as file:
            line = file.readline()
            cnt = 0
            article_text = ""
            article_title = None
            article_id = None
            reading_text = False
            reading_revision = False
            while line and (not limit or cnt < limit):
                if cnt % 1000000 == 0:
                    print(datetime.datetime.now(), "processed", cnt, "lines of Wikipedia dump")
                clean_line = line.strip().decode("utf-8")
                # print(clean_line)

                if clean_line == "<revision>":
                    reading_revision = True
                elif clean_line == "</revision>":
                    reading_revision = False

                # Start reading new page
                if clean_line == "<page>":
                    article_text = ""
                    article_title = None
                    article_id = None

                # finished reading this page
                elif clean_line == "</page>":
                    if article_id:
                        try:
                            _process_wp_text(wp_to_id, entityfile, article_id, article_title, article_text.strip(), training_output)
                        except Exception as e:
                            print("Error processing article", article_id, article_title, e)
                    else:
                        print("Done processing a page, but couldn't find an article_id ?", article_title)
                    article_text = ""
                    article_title = None
                    article_id = None
                    reading_text = False
                    reading_revision = False

                # start reading text within a page
                if "<text" in clean_line:
                    reading_text = True

                if reading_text:
                    article_text += " " + clean_line

                # stop reading text within a page (we assume a new page doesn't start on the same line)
                if "</text" in clean_line:
                    reading_text = False

                # read the ID of this article (outside the revision portion of the document)
                if not reading_revision:
                    ids = id_regex.search(clean_line)
                    if ids:
                        article_id = ids[0]
                        if article_id in read_ids:
                            print("Found duplicate article ID", article_id, clean_line)  # This should never happen ...
                        read_ids.add(article_id)

                # read the title of this article  (outside the revision portion of the document)
                if not reading_revision:
                    titles = title_regex.search(clean_line)
                    if titles:
                        article_title = titles[0].strip()

                line = file.readline()
                cnt += 1


text_regex = re.compile(r'(?<=<text xml:space=\"preserve\">).*(?=</text)')


def _process_wp_text(wp_to_id, entityfile, article_id, article_title, article_text, training_output):
    found_entities = False
    # print("Processing", article_id, article_title)

    # ignore meta Wikipedia pages
    if article_title.startswith("Wikipedia:"):
        return

    # remove the text tags
    text = text_regex.search(article_text).group(0)

    # stop processing if this is a redirect page
    if text.startswith("#REDIRECT"):
        return

    # print()
    # print(text)

    # get the raw text without markup etc, keeping only interwiki links
    clean_text = _get_clean_wp_text(text)
    # print()
    # print(clean_text)

    # read the text char by char to get the right offsets of the interwiki links
    final_text = ""
    open_read = 0
    reading_text = True
    reading_entity = False
    reading_mention = False
    reading_special_case = False
    entity_buffer = ""
    mention_buffer = ""
    for index, letter in enumerate(clean_text):
        if letter == '[':
            open_read += 1
        elif letter == ']':
            open_read -= 1
        elif letter == '|':
            if reading_text:
                final_text += letter
            # switch from reading entity to mention in the [[entity|mention]] pattern
            elif reading_entity:
                reading_text = False
                reading_entity = False
                reading_mention = True
            else:
                reading_special_case = True
        else:
            if reading_entity:
                entity_buffer += letter
            elif reading_mention:
                mention_buffer += letter
            elif reading_text:
                final_text += letter
            else:
                raise ValueError("Not sure at point", clean_text[index-2:index+2])

        if open_read > 2:
            reading_special_case = True

        if open_read == 2 and reading_text:
            reading_text = False
            reading_entity = True
            reading_mention = False

        # we just finished reading an entity
        if open_read == 0 and not reading_text:
            if '#' in entity_buffer or entity_buffer.startswith(':'):
                reading_special_case = True
            # Ignore cases with nested structures like File: handles etc
            if not reading_special_case:
                if not mention_buffer:
                    mention_buffer = entity_buffer
                start = len(final_text)
                end = start + len(mention_buffer)
                qid = wp_to_id.get(entity_buffer, None)
                if qid:
                    _write_training_entity(outputfile=entityfile,
                                           article_id=article_id,
                                           alias=mention_buffer,
                                           entity=qid,
                                           start=start,
                                           end=end)
                found_entities = True
                final_text += mention_buffer

            entity_buffer = ""
            mention_buffer = ""

            reading_text = True
            reading_entity = False
            reading_mention = False
            reading_special_case = False

    if found_entities:
        _write_training_article(article_id=article_id, clean_text=final_text, training_output=training_output)


info_regex = re.compile(r'{[^{]*?}')
htlm_regex = re.compile(r'&lt;!--[^-]*--&gt;')
category_regex = re.compile(r'\[\[Category:[^\[]*]]')
file_regex = re.compile(r'\[\[File:[^[\]]+]]')
ref_regex = re.compile(r'&lt;ref.*?&gt;')     # non-greedy
ref_2_regex = re.compile(r'&lt;/ref.*?&gt;')  # non-greedy


def _get_clean_wp_text(article_text):
    clean_text = article_text.strip()

    # remove bolding & italic markup
    clean_text = clean_text.replace('\'\'\'', '')
    clean_text = clean_text.replace('\'\'', '')

    # remove nested {{info}} statements by removing the inner/smallest ones first and iterating
    try_again = True
    previous_length = len(clean_text)
    while try_again:
        clean_text = info_regex.sub('', clean_text)  # non-greedy match excluding a nested {
        if len(clean_text) < previous_length:
            try_again = True
        else:
            try_again = False
        previous_length = len(clean_text)

    # remove HTML comments
    clean_text = htlm_regex.sub('', clean_text)

    # remove Category and File statements
    clean_text = category_regex.sub('', clean_text)
    clean_text = file_regex.sub('', clean_text)

    # remove multiple =
    while '==' in clean_text:
        clean_text = clean_text.replace("==", "=")

    clean_text = clean_text.replace(". =", ".")
    clean_text = clean_text.replace(" = ", ". ")
    clean_text = clean_text.replace("= ", ".")
    clean_text = clean_text.replace(" =", "")

    # remove refs (non-greedy match)
    clean_text = ref_regex.sub('', clean_text)
    clean_text = ref_2_regex.sub('', clean_text)

    # remove additional wikiformatting
    clean_text = re.sub(r'&lt;blockquote&gt;', '', clean_text)
    clean_text = re.sub(r'&lt;/blockquote&gt;', '', clean_text)

    # change special characters back to normal ones
    clean_text = clean_text.replace(r'&lt;', '<')
    clean_text = clean_text.replace(r'&gt;', '>')
    clean_text = clean_text.replace(r'&quot;', '"')
    clean_text = clean_text.replace(r'&amp;nbsp;', ' ')
    clean_text = clean_text.replace(r'&amp;', '&')

    # remove multiple spaces
    while '  ' in clean_text:
        clean_text = clean_text.replace('  ', ' ')

    return clean_text.strip()


def _write_training_article(article_id, clean_text, training_output):
    file_loc = training_output + "/" + str(article_id) + ".txt"
    with open(file_loc, mode='w', encoding='utf8') as outputfile:
        outputfile.write(clean_text)


def _write_training_entity(outputfile, article_id, alias, entity, start, end):
    outputfile.write(article_id + "|" + alias + "|" + entity + "|" + str(start) + "|" + str(end) + "\n")


def read_training_entities(training_output):
    entityfile_loc = training_output + "/" + ENTITY_FILE
    entries_per_article = dict()

    with open(entityfile_loc, mode='r', encoding='utf8') as file:
        for line in file:
            fields = line.replace('\n', "").split(sep='|')
            article_id = fields[0]
            alias = fields[1]
            wp_title = fields[2]
            start = fields[3]
            end = fields[4]

            entries_by_offset = entries_per_article.get(article_id, dict())
            entries_by_offset[start + "-" + end] = (alias, wp_title)

            entries_per_article[article_id] = entries_by_offset

    return entries_per_article


def read_training(nlp, training_dir, dev, limit, to_print):
    # This method will provide training examples that correspond to the entity annotations found by the nlp object
    entries_per_article = read_training_entities(training_output=training_dir)

    data = []

    cnt = 0
    files = listdir(training_dir)
    for f in files:
        if not limit or cnt < limit:
            if dev == run_el.is_dev(f):
                article_id = f.replace(".txt", "")
                if cnt % 500 == 0 and to_print:
                    print(datetime.datetime.now(), "processed", cnt, "files in the training dataset")

                try:
                    # parse the article text
                    with open(os.path.join(training_dir, f), mode="r", encoding='utf8') as file:
                        text = file.read()
                        article_doc = nlp(text)

                    entries_by_offset = entries_per_article.get(article_id, dict())

                    gold_entities = list()
                    for ent in article_doc.ents:
                        start = ent.start_char
                        end = ent.end_char

                        entity_tuple = entries_by_offset.get(str(start) + "-" + str(end), None)
                        if entity_tuple:
                            alias, wp_title = entity_tuple
                            if ent.text != alias:
                                print("Non-matching entity in", article_id, start, end)
                            else:
                                gold_entities.append((start, end, wp_title))

                    if gold_entities:
                        gold = GoldParse(doc=article_doc, links=gold_entities)
                        data.append((article_doc, gold))

                    cnt += 1
                except Exception as e:
                    print("Problem parsing article", article_id)
                    print(e)
                    raise e

    if to_print:
        print()
        print("Processed", cnt, "training articles, dev=" + str(dev))
        print()
    return data
