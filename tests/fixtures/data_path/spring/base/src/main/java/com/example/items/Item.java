package com.example.items;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;

@Entity
public class Item {
    @Id
    private Long id;
    private String name;

    protected Item() {}

    public Item(String name) {
        this.name = name;
    }

    public void setName(String name) {
        this.name = name;
    }
}
