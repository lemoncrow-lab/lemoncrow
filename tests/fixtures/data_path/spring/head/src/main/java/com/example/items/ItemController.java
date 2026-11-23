package com.example.items;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/items")
public class ItemController {
    private static final Logger log = LoggerFactory.getLogger(ItemController.class);
    private final ItemService service;
    private final ItemRepository itemRepository;

    public ItemController(ItemService service, ItemRepository itemRepository) {
        this.service = service;
        this.itemRepository = itemRepository;
    }

    @PostMapping
    public void create(@RequestBody ItemRequest body) {
        service.create(Names.clean(body.getName()));
    }

    @PutMapping("/{id}")
    public void rename(@PathVariable long id) {
        Item item = itemRepository.findById(id).orElseThrow();
        item.setName("renamed");
        itemRepository.save(item);
    }

    @GetMapping("/{id}")
    public Item show(@PathVariable long id) {
        log.info("show {}", id);
        return itemRepository.findById(id).orElseThrow();
    }
}
